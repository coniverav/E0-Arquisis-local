"""E2-068 / AD5: recuperación conservadora de un HTTP 202 perdido.

El estado del swap se obtiene únicamente de Wallet. Un GET con 404, timeout,
error o respuesta no reconocida NO demuestra que el POST original no ocurrió.
Nunca se crea una operación ni una Idempotency-Key nueva desde aquí.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from app.models import SwapRecord
from app.services.wallet_client import WalletError
from app.services.wallet_service import WalletService

logger = logging.getLogger(__name__)

TERMINAL_STATES = frozenset({"SETTLED", "REJECTED", "CANCELLED", "EXPIRED"})
WALLET_STATES = frozenset({"proposed", "accepted", "settled", "rejected", "cancelled", "expired"})
EVENT_FOR_STATUS = {
    "settled": "swap-settled",
    "rejected": "swap-rejected",
    "cancelled": "swap-cancelled",
    "expired": "swap-expired",
}


@dataclass(frozen=True)
class WalletObservation:
    swap_id: str
    status: str
    reason: str | None = None
    token: str | None = None


async def inspect_known_swap(client: Any, swap_id: str | None) -> WalletObservation | None:
    """Consulta por swapId conocido; los resultados no verificables son inciertos.

    No existe GET por Idempotency-Key en el contrato oficial: si falta swapId
    solo se puede esperar evento/reintentar el mismo POST con la MISMA llave.
    """
    if not swap_id:
        return None
    try:
        response = await client.get_swap(swap_id)
    except WalletError as exc:
        logger.warning("E2-068: consulta wallet incierta swap=%s code=%s",
                       swap_id, exc.status_code)
        return None
    if not isinstance(response, dict):
        return None
    identity = response.get("swapId", response.get("id"))
    if identity is not None and identity != swap_id:
        logger.warning("E2-068: GET wallet devolvió una identidad distinta")
        return None
    status = response.get("status")
    if not isinstance(status, str) or status.lower() not in WALLET_STATES:
        return None
    reason = response.get("reason")
    token = response.get("token")
    return WalletObservation(
        swap_id=swap_id,
        status=status.lower(),
        reason=reason if isinstance(reason, str) else None,
        token=token if isinstance(token, str) and token else None,
    )


def apply_wallet_observation(
    session: Session,
    *,
    record: SwapRecord,
    observation: WalletObservation,
    now: datetime | None = None,
) -> bool:
    """Converge estados conocidos sin perder las transiciones de eventos.

    True = el GET demuestra que NO hace falta reenviar el POST.
    False = GET no demuestra la ejecución de esa operación; puede reintentarse
    con la misma llave, sujeto a los plazos oficiales.
    """
    if not record.swap_id or record.swap_id != observation.swap_id:
        return False
    if record.status in TERMINAL_STATES:
        return True

    if observation.status in EVENT_FOR_STATUS:
        data: dict[str, Any] = {
            "swapId": record.swap_id,
            "idpk": record.idpk,
        }
        if observation.reason is not None:
            data["reason"] = observation.reason
        # Hecho definitivo de Wallet: mismo procedimiento idempotente que
        # procesa los eventos AMQP; se aplican ledger/holds una sola vez.
        WalletService().process_swap_event(
            session, event_type=EVENT_FOR_STATUS[observation.status], data=data,
        )
        return True

    if record.role == "PROPOSER" and observation.status in {"proposed", "accepted"}:
        # La propuesta existe. Nunca se emite otro POST por ese mismo swap.
        # La respuesta comercial sigue requiriendo el evento Wallet con JWT.
        if observation.token:
            WalletService().process_swap_event(
                session,
                event_type="swap-proposed",
                data={"swapId": record.swap_id, "idpk": record.idpk,
                      "token": observation.token},
            )
        else:
            record.status = "PROPOSED"
            session.add(record)
            session.flush()
        return True

    if record.role == "ACCEPTOR" and observation.status == "accepted":
        # El GET informa aceptación de wallet pero no liquidación económica.
        record.status = "ACCEPT_SUBMITTED"
        record.accept_error = None
        session.add(record)
        session.flush()
        return True

    # Para el ACCEPTOR, "proposed" solo confirma la propuesta del vendedor:
    # NO prueba que nuestro POST /accept se haya procesado.
    return False
