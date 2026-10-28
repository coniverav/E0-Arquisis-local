"""Servicio de operaciones durables con la Wallet Central (E2-020, E2-021, E2-022).

Implementa:
- Gestión durable de Idempotency-Key (UUIDv4) en Postgres: si falla la conexión o hay timeout,
  se recupera o reintenta con la misma clave exacta.
- Registro en base de datos del estado SwapRecord antes de llamar a la wallet.
- Procesamiento de respuestas HTTP 202 sin asumir liquidación (AD5).
- Consumo y correlación de eventos swap-* (`swap-proposed`, `swap-settled`, `swap-rejected`,
  `swap-expired`, `swap-cancelled`) por data.idpk y swapId.
- Actualización de balances wallet (available/held/creditLine) respetando a la wallet como
  autoridad económica (E2-022, AD5).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
from typing import Any, Optional
import uuid

from sqlmodel import Session, select

from app.models import (
    SwapRecord,
    WalletBalanceSnapshot,
    OfferReservation,
    MarketOffer,
)
from app.services.wallet_client import (
    WalletClient,
    WalletError,
    WalletTimeoutError,
    WalletSwapWriteResponse,
    WalletBalanceResponse,
)

logger = logging.getLogger(__name__)


class WalletService:
    """Gestiona el ciclo de vida de los swaps y sincronización con la Wallet."""

    def __init__(self, client: Optional[WalletClient] = None):
        self.client = client or WalletClient()

    def get_or_create_swap_record(
        self,
        session: Session,
        *,
        idpk: str,
        cycle_id: str,
        role: str,
        counterpart_city_id: str,
        give_asset: str,
        give_amount: Decimal,
        take_asset: str,
        take_amount: Decimal,
        memo: Optional[str] = None,
        swap_id: Optional[str] = None,
        source_bid_idpk: Optional[str] = None,
    ) -> SwapRecord:
        """
        Obtiene un SwapRecord existente o crea uno nuevo con Idempotency-Key durable.
        Garantiza que llamadas repetidas con el mismo idpk reutilicen la misma llave.
        """
        existing = session.exec(
            select(SwapRecord).where(SwapRecord.idpk == idpk)
        ).first()

        if existing:
            return existing

        now = datetime.now(timezone.utc)
        record = SwapRecord(
            idpk=idpk,
            swap_id=swap_id,
            cycle_id=cycle_id,
            role=role,
            counterpart_city_id=counterpart_city_id,
            give_asset=give_asset,
            give_amount=give_amount,
            take_asset=take_asset,
            take_amount=take_amount,
            memo=memo,
            status="SUBMITTED",
            idempotency_key=str(uuid.uuid4()),
            source_bid_idpk=source_bid_idpk,
            created_at=now,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    async def execute_propose_swap(
        self,
        session: Session,
        record: SwapRecord,
    ) -> WalletSwapWriteResponse:
        """
        Envía la propuesta de swap a la wallet usando la idempotency_key durable persistida.
        """
        logger.info(
            "Enviando propuesta de swap a la wallet: idpk=%s key=%s",
            record.idpk, record.idempotency_key
        )
        try:
            resp = await self.client.propose_swap(
                idempotency_key=record.idempotency_key,
                give_asset=record.give_asset,
                give_amount=record.give_amount,
                take_asset=record.take_asset,
                take_amount=record.take_amount,
                memo=record.memo or "",
                expires_at=record.expires_at or (
                    datetime.now(timezone.utc) + timedelta(seconds=60)
                ),
            )
            # Si la wallet responde con swapId en el cuerpo HTTP 202, lo asociamos preliminarmente
            swap_id_in_resp = resp.data.get("swapId") or resp.data.get("id")
            if swap_id_in_resp and not record.swap_id:
                record.swap_id = swap_id_in_resp
                session.add(record)
                session.commit()
            return resp
        except WalletTimeoutError as exc:
            # En caso de timeout o red rota, el record mantiene su idempotency_key para reintentos posteriores
            logger.warning(
                "Timeout enviando swap idpk=%s con key=%s. La operación sigue SUBMITTED para recuperación: %s",
                record.idpk, record.idempotency_key, exc
            )
            raise

    async def execute_accept_swap(
        self,
        session: Session,
        record: SwapRecord,
    ) -> WalletSwapWriteResponse:
        """
        Acepta un swap en la wallet usando accept_idempotency_key durable (E2-020).
        """
        if not record.swap_id:
            raise ValueError(f"No se puede aceptar swap {record.idpk} sin swap_id asignado")

        if not record.accept_idempotency_key:
            record.accept_idempotency_key = str(uuid.uuid4())
            record.status = "ACCEPT_SUBMITTED"
            session.add(record)
            session.commit()

        try:
            resp = await self.client.accept_swap(
                record.swap_id,
                idempotency_key=record.accept_idempotency_key,
            )
            return resp
        except WalletTimeoutError as exc:
            logger.warning(
                "Timeout aceptando swap %s (key=%s): %s",
                record.swap_id, record.accept_idempotency_key, exc
            )
            raise

    def process_swap_event(
        self,
        session: Session,
        *,
        event_type: str,
        data: dict[str, Any],
    ) -> Optional[SwapRecord]:
        """
        Procesa eventos de wallet de RabbitMQ (E2-021):
        - swap-proposed
        - swap-settled
        - swap-rejected
        - swap-expired
        - swap-cancelled

        Correlaciona por data.swapId, data.idpk y llaves de idempotencia
        (proposición y aceptación), aplicando transiciones de estado y
        liberando o confirmando reservas de energía según AD4 / AD5.

        El `swapId` manda: el `idpk` de un `swap-settled` es la llave de la
        operación que lo causó (normalmente la aceptación), que no tiene por
        qué coincidir con el `idpk` local del proponente.
        """
        idpk = data.get("idpk")
        swap_id = data.get("swapId") or data.get("id")

        record: Optional[SwapRecord] = None
        if swap_id:
            record = session.exec(
                select(SwapRecord).where(SwapRecord.swap_id == swap_id)
            ).first()
        if record is None and idpk:
            record = session.exec(
                select(SwapRecord).where(SwapRecord.idpk == idpk)
            ).first()
        if record is None and idpk:
            record = session.exec(
                select(SwapRecord).where(
                    SwapRecord.accept_idempotency_key == idpk
                )
            ).first()
        if record is None and idpk:
            record = session.exec(
                select(SwapRecord).where(SwapRecord.idempotency_key == idpk)
            ).first()

        if record is None:
            if not idpk and not swap_id:
                logger.warning("Evento %s sin idpk ni swapId en payload", event_type)
            else:
                logger.warning(
                    "SwapRecord no encontrado para evento %s (idpk=%s, swap_id=%s)",
                    event_type, idpk, swap_id
                )
            return None

        # E2-029: un mismo swap terminal no debe descontar la oferta dos veces
        # aunque el evento llegue con otro envelope (o se reprocese).
        if record.status in {"SETTLED", "REJECTED", "EXPIRED", "CANCELLED"}:
            return record

        now = datetime.now(timezone.utc)
        if swap_id and not record.swap_id:
            record.swap_id = swap_id

        # Actualización de token o expiración si viene en el evento
        if "swapToken" in data:
            record.swap_token = data["swapToken"]
        if "expiresAt" in data and isinstance(data["expiresAt"], str):
            try:
                record.expires_at = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            except ValueError:
                pass

        if event_type == "swap-proposed":
            record.status = "PROPOSED"

        elif event_type == "swap-settled":
            record.status = "SETTLED"
            record.settled_at = now
            # Confirmar reserva asociada si somos vendedores (AD4)
            if record.memo:
                self._update_offer_and_reservation(
                    session,
                    offer_id=record.memo,
                    bid_idpk=record.idpk,
                    action="COMMIT",
                )

        elif event_type in ("swap-rejected", "swap-expired", "swap-cancelled"):
            if event_type == "swap-rejected":
                record.status = "REJECTED"
                record.rejection_reason = data.get("reason", "UNKNOWN_REJECTION")
            elif event_type == "swap-expired":
                record.status = "EXPIRED"
            else:
                record.status = "CANCELLED"

            # Liberar reserva de energía si existía retención local (AD4)
            if record.memo:
                self._update_offer_and_reservation(
                    session,
                    offer_id=record.memo,
                    bid_idpk=record.idpk,
                    action="RELEASE",
                )

        session.add(record)

        # El snapshot se agrega antes del commit para que la liquidación del
        # swap, la reserva y los balances queden en la misma transacción.
        if isinstance(data.get("balancesAfter"), dict):
            self.record_balance_snapshot(
                session,
                cycle_id=record.cycle_id,
                source="EVENT",
                balances_dict=data["balancesAfter"],
            )

        session.flush()
        session.refresh(record)

        return record

    def _update_offer_and_reservation(
        self,
        session: Session,
        *,
        offer_id: str,
        bid_idpk: str,
        action: str,  # "COMMIT" o "RELEASE"
    ) -> None:
        """Actualiza la reserva de oferta según la confirmación o cancelación del swap."""
        res = session.exec(
            select(OfferReservation).where(
                OfferReservation.offer_id == offer_id,
                OfferReservation.bid_idpk == bid_idpk,
            ).with_for_update()
        ).first()

        if res is None or res.status != "HOLD":
            # Reserva inexistente o ya terminal: no descontar otra vez.
            return

        now = datetime.now(timezone.utc)
        if action == "COMMIT":
            offer = session.exec(
                select(MarketOffer).where(
                    MarketOffer.offer_id == offer_id
                ).with_for_update()
            ).first()
            res.status = "COMMITTED"
            if offer is not None:
                offer.remaining_quantity = max(0, offer.remaining_quantity - res.quantity)
                offer.last_updated_at = now
                session.add(offer)
                # El settlement de Wallet es autoridad; no el HTTP 202.
                from app.services.seller_offer_events import enqueue_confirmed_offer_update
                enqueue_confirmed_offer_update(session, offer, now=now)
        elif action == "RELEASE":
            res.status = "RELEASED"
            res.released_at = now
        else:
            raise ValueError("Acción de reserva no reconocida")
        session.add(res)
        session.flush()

    def record_balance_snapshot(
        self,
        session: Session,
        *,
        cycle_id: Optional[str],
        source: str,
        balances_dict: dict[str, Any],
    ) -> WalletBalanceSnapshot:
        """
        Guarda un snapshot de los balances informado por la wallet (E2-022).

        El contrato v3.0.1 (`common/balances-after`) usa `CR`/`KWH` en
        mayúscula y no incluye `creditLine`. Un evento no puede inventar la
        línea de crédito, así que se persiste `NULL`.
        """
        now = datetime.now(timezone.utc)
        cr = balances_dict.get("CR")
        kwh = balances_dict.get("KWH")

        if not isinstance(cr, dict) or not isinstance(kwh, dict):
            raise ValueError(
                "balancesAfter debe traer CR y KWH en mayúscula "
                "(contrato v3.0.1); no se aceptan kwh/cr"
            )

        def amount(asset: str, values: dict[str, Any], key: str) -> Decimal:
            if key not in values:
                raise ValueError(
                    f"balancesAfter.{asset}.{key} es obligatorio"
                )
            try:
                return Decimal(str(values[key]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(
                    f"balancesAfter.{asset}.{key} debe ser numérico"
                ) from exc

        snapshot = WalletBalanceSnapshot(
            cycle_id=cycle_id,
            source=source,
            kwh_available=amount("KWH", kwh, "available"),
            kwh_held=amount("KWH", kwh, "held"),
            cr_available=amount("CR", cr, "available"),
            cr_held=amount("CR", cr, "held"),
            credit_line=None,
            recorded_at=now,
        )
        session.add(snapshot)
        session.flush()
        return snapshot

    async def sync_balance_from_wallet(
        self,
        session: Session,
        cycle_id: Optional[str] = None,
    ) -> WalletBalanceSnapshot:
        """Consulta GET /balance a la central y persiste el snapshot más reciente."""
        bal: WalletBalanceResponse = await self.client.get_balance()
        now = datetime.now(timezone.utc)
        snapshot = WalletBalanceSnapshot(
            cycle_id=cycle_id,
            source="REST_SYNC",
            kwh_available=bal.kwh_available,
            kwh_held=bal.kwh_held,
            cr_available=bal.cr_available,
            cr_held=bal.cr_held,
            credit_line=bal.credit_line,
            recorded_at=now,
        )
        session.add(snapshot)
        session.commit()
        session.refresh(snapshot)
        return snapshot
