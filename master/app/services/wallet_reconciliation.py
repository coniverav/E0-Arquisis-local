"""E2-044: conciliación del ledger local con `GET /balance` y `/statement`.

La wallet es la autoridad económica. Este módulo:

1. Consulta el saldo oficial y guarda un snapshot `REST_SYNC`.
2. Recorre todas las páginas de `GET /statement` (`entries` + `nextCursor`)
   y persiste los asientos con deduplicación por `entryId`.
3. Compara el último snapshot `EVENT` (derivado de `swap-settled`) con el
   saldo oficial y explica la diferencia con los asientos posteriores y los
   hechos `trade-recorded` / `imbalance-settlement`.

El resultado se guarda con `authority="wallet"`: un snapshot local nunca se
declara autoridad ni corrige a la wallet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Any, Optional

from sqlmodel import Session, select

from app.config import CITY_ID, WALLET_RECONCILE_INTERVAL_SECONDS
from app.database import engine
from app.models import (
    ImbalanceSettlement,
    TradeRecord,
    WalletBalanceSnapshot,
    WalletReconciliation,
    WalletStatementEntry,
)
from app.services.wallet_client import WalletClient, WalletError, WalletStatementPage

logger = logging.getLogger(__name__)

_last_tick_monotonic: float | None = None


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WalletError("asiento de statement con monto no numérico") from exc
    if not result.is_finite():
        raise WalletError("asiento de statement con monto no finito")
    return result


async def fetch_statement_entries(
    client: WalletClient,
    *,
    cycle_id: Optional[str] = None,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Recorre `GET /statement` hasta `nextCursor = null`."""

    entries: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        page: WalletStatementPage = await client.get_statement(
            cycle_id=cycle_id, cursor=cursor,
        )
        entries.extend(page.entries)
        cursor = page.next_cursor
        if cursor is None:
            return entries
        if cursor in seen_cursors:
            raise WalletError("GET /statement repitió el mismo nextCursor")
        seen_cursors.add(cursor)

    raise WalletError("GET /statement excedió el máximo de páginas")


def store_statement_entries(
    session: Session,
    entries: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> int:
    """Persiste asientos nuevos; devuelve cuántos no existían."""

    fetched_at = now or datetime.now(timezone.utc)
    created = 0

    for entry in entries:
        entry_id = entry.get("entryId")
        if not isinstance(entry_id, str) or not entry_id:
            raise WalletError("statement entry sin entryId")

        existing = session.exec(
            select(WalletStatementEntry).where(
                WalletStatementEntry.entry_id == entry_id
            )
        ).first()
        if existing is not None:
            continue

        asset = entry.get("asset")
        kind = entry.get("kind")
        created_at = _parse_instant(entry.get("createdAt"))
        if asset not in {"CR", "KWH"} or not isinstance(kind, str) or created_at is None:
            raise WalletError(f"statement entry {entry_id} inválido")
        reference = entry.get("reference")
        if not isinstance(reference, str) or not reference:
            raise WalletError(f"statement entry {entry_id} sin reference")

        session.add(WalletStatementEntry(
            entry_id=entry_id,
            cycle_id=entry.get("cycleId"),
            asset=asset,
            kind=kind,
            amount=_decimal(entry.get("amount")),
            counterpart=entry.get("counterparty"),
            reference=reference,
            created_at=created_at,
            fetched_at=fetched_at,
        ))
        created += 1

    session.flush()
    return created


def _latest_event_snapshot(
    session: Session,
    *,
    before: Optional[datetime] = None,
) -> Optional[WalletBalanceSnapshot]:
    query = select(WalletBalanceSnapshot).where(
        WalletBalanceSnapshot.source == "EVENT"
    )
    if before is not None:
        query = query.where(WalletBalanceSnapshot.recorded_at <= before)
    return session.exec(
        query.order_by(WalletBalanceSnapshot.recorded_at.desc())
    ).first()


def _sum_statement(
    session: Session,
    *,
    after: datetime,
    cycle_id: Optional[str] = None,
) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
    query = select(WalletStatementEntry).where(
        WalletStatementEntry.created_at > after
    )
    if cycle_id is not None:
        query = query.where(WalletStatementEntry.cycle_id == cycle_id)

    totals = {"CR": Decimal("0"), "KWH": Decimal("0")}
    by_kind: dict[str, dict[str, Decimal]] = {"CR": {}, "KWH": {}}
    for entry in session.exec(query).all():
        totals[entry.asset] += entry.amount
        bucket = by_kind[entry.asset]
        bucket[entry.kind] = bucket.get(entry.kind, Decimal("0")) + entry.amount
    return totals, by_kind


def _local_fact_counts(
    session: Session,
    *,
    after: datetime,
    cycle_id: Optional[str],
) -> dict[str, int]:
    trade_query = select(TradeRecord).where(TradeRecord.recorded_at > after)
    imbalance_query = select(ImbalanceSettlement).where(
        ImbalanceSettlement.recorded_at > after
    )
    if cycle_id is not None:
        trade_query = trade_query.where(TradeRecord.cycle_id == cycle_id)
        imbalance_query = imbalance_query.where(
            ImbalanceSettlement.cycle_id == cycle_id
        )
    return {
        "trade_records": len(session.exec(trade_query).all()),
        "imbalance_settlements": len(session.exec(imbalance_query).all()),
    }


async def reconcile_wallet(
    session: Session,
    *,
    client: Optional[WalletClient] = None,
    cycle_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> WalletReconciliation:
    """Compara ledger local y wallet, persiste snapshot, asientos y reporte."""

    current = now or datetime.now(timezone.utc)
    wallet_client = client or WalletClient()

    balance = await wallet_client.get_balance()
    wallet_as_of = _parse_instant(balance.timestamp) or current

    # 1. Snapshot oficial (REST_SYNC). La wallet manda.
    snapshot = WalletBalanceSnapshot(
        cycle_id=cycle_id,
        source="REST_SYNC",
        kwh_available=balance.kwh_available,
        kwh_held=balance.kwh_held,
        cr_available=balance.cr_available,
        cr_held=balance.cr_held,
        credit_line=balance.credit_line,
        recorded_at=current,
    )
    session.add(snapshot)

    # 2. Asientos del statement, deduplicados por entryId.
    entries = await fetch_statement_entries(wallet_client)
    new_entries = store_statement_entries(session, entries, now=current)

    # 3. Comparación contra el último saldo derivado de eventos.
    baseline = _latest_event_snapshot(session, before=wallet_as_of)
    if baseline is None:
        baseline = _latest_event_snapshot(session)

    status = "NO_LOCAL_BASELINE"
    cr_local = kwh_local = None
    cr_difference = kwh_difference = None
    cr_explained = kwh_explained = None
    cr_unexplained = kwh_unexplained = None
    explanation: dict[str, Any] = {
        "city_id": CITY_ID,
        "authority": "wallet",
        "wallet_as_of": wallet_as_of.isoformat(),
        "statement_entries_total": len(entries),
        "statement_entries_new": new_entries,
        "cycle_id": cycle_id,
    }

    if baseline is not None:
        cr_local = baseline.cr_available
        kwh_local = baseline.kwh_available

        explained, by_kind = _sum_statement(
            session, after=baseline.recorded_at, cycle_id=None,
        )
        cr_explained = explained["CR"]
        kwh_explained = explained["KWH"]

        cr_difference = balance.cr_available - cr_local
        kwh_difference = balance.kwh_available - kwh_local

        cr_unexplained = cr_difference - cr_explained
        kwh_unexplained = kwh_difference - kwh_explained

        if cr_unexplained == 0 and kwh_unexplained == 0:
            status = (
                "MATCH"
                if cr_explained == 0 and kwh_explained == 0
                else "EXPLAINED"
            )
        else:
            status = "DIVERGENT"

        explanation.update({
            "baseline_snapshot_id": baseline.id,
            "baseline_recorded_at": baseline.recorded_at.isoformat(),
            "statement_by_kind": {
                asset: {kind: str(value) for kind, value in values.items()}
                for asset, values in by_kind.items()
            },
            "local_facts": _local_fact_counts(
                session, after=baseline.recorded_at, cycle_id=cycle_id,
            ),
        })

    report = WalletReconciliation(
        cycle_id=cycle_id,
        status=status,
        authority="wallet",
        cr_available_local=cr_local,
        cr_available_wallet=balance.cr_available,
        cr_difference=cr_difference,
        cr_explained=cr_explained,
        cr_unexplained=cr_unexplained,
        kwh_available_local=kwh_local,
        kwh_available_wallet=balance.kwh_available,
        kwh_difference=kwh_difference,
        kwh_explained=kwh_explained,
        kwh_unexplained=kwh_unexplained,
        explanation=explanation,
        created_at=current,
    )
    session.add(report)
    session.commit()
    session.refresh(report)

    if status == "DIVERGENT":
        logger.error(
            "Conciliación wallet divergente: CR diff=%s no explicado=%s; "
            "KWH diff=%s no explicado=%s",
            cr_difference, cr_unexplained, kwh_difference, kwh_unexplained,
        )
    else:
        logger.info("Conciliación wallet: %s", status)

    return report


async def reconcile_wallet_tick(
    *,
    interval_seconds: Optional[float] = None,
    client: Optional[WalletClient] = None,
    now: Optional[datetime] = None,
) -> Optional[WalletReconciliation]:
    """Ejecuta la conciliación como mucho una vez por intervalo.

    El scheduler puede llamarla en cada vuelta sin golpear la wallet cada
    pocos segundos. Devuelve `None` si todavía no corresponde o si la wallet
    no está disponible (el tick nunca tumba al scheduler).
    """

    global _last_tick_monotonic

    interval = (
        WALLET_RECONCILE_INTERVAL_SECONDS
        if interval_seconds is None
        else interval_seconds
    )
    current_tick = time.monotonic()
    if (_last_tick_monotonic is not None and interval > 0
            and current_tick - _last_tick_monotonic < interval):
        return None

    _last_tick_monotonic = current_tick
    try:
        with Session(engine) as session:
            return await reconcile_wallet(session, client=client, now=now)
    except (WalletError, OSError) as exc:
        logger.warning("Conciliación wallet no disponible: %s", exc)
        return None
    except Exception:
        logger.exception("Error inesperado en la conciliación wallet")
        return None
