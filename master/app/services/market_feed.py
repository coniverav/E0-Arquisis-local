"""Aplica el feed al snapshot; inbound_messages conserva cada recepción."""

from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.models import InboundMessage, MarketCycle, MarketEventClaim, MarketFeedOffer
from app.schemas import MarketFeedMessageIn
from app.services.message_audit import (
    INBOUND_DUPLICATE,
    INBOUND_NOT_PROCESSED,
    INBOUND_PROCESSED,
    mark_inbound_result,
    record_inbound_message,
)


def _fingerprint(payload: dict) -> str:
    # Un retry puede tener otro msgId/timestamp, pero debe describir la misma operación.
    operation = {key: value for key, value in payload.items()
                 if key not in {"msgId", "timestamp"}}
    encoded = json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _lock_market_cycle(session: Session, cycle_id: str) -> MarketCycle:
    # La PK serializa altas simultáneas; FOR UPDATE serializa todos los eventos
    # de un ciclo entre las dos réplicas de master.
    session.execute(insert(MarketCycle).values(cycle_id=cycle_id).on_conflict_do_nothing())
    return session.exec(select(MarketCycle).where(
        MarketCycle.cycle_id == cycle_id
    ).with_for_update()).one()


def _offer_snapshot(session: Session, cycle_id: str, city_id: str) -> MarketFeedOffer | None:
    return session.exec(select(MarketFeedOffer).where(
        MarketFeedOffer.cycle_id == cycle_id,
        MarketFeedOffer.city_id == city_id,
    )).first()


def _apply_offer_event(
    session: Session,
    payload: MarketFeedMessageIn,
    cycle: MarketCycle,
) -> tuple[str, str | None]:
    if cycle.closed_at is not None:
        return INBOUND_NOT_PROCESSED, "El mercado de este ciclo ya cerró"

    snapshot = _offer_snapshot(session, payload.cycleId, payload.cityId)
    offer_id = payload.data["offerId"]
    event_time = payload.timestamp

    if snapshot is not None and snapshot.offer_id != offer_id:
        snapshot.uncertain = True
        session.add(snapshot)
        return INBOUND_NOT_PROCESSED, "Otra oferta ya ocupa esta ciudad y ciclo"

    if payload.type == "offer":
        if snapshot is None:
            session.add(MarketFeedOffer(
                cycle_id=payload.cycleId, city_id=payload.cityId, offer_id=offer_id,
                initial_quantity=payload.data["quantity"],
                remaining=payload.data["quantity"],
                price_per_energy=Decimal(str(payload.data["pricePerEnergy"])),
                state="ACTIVE", uncertain=False,
                last_event_at=event_time, last_event_msg_id=str(payload.msgId),
            ))
            return INBOUND_PROCESSED, None

        if snapshot.state == "UNRESOLVED" and event_time < snapshot.last_event_at:
            # Completa la base sin pisar el update observado después.
            snapshot.initial_quantity = payload.data["quantity"]
            snapshot.state = "ACTIVE"
            snapshot.uncertain = True
            session.add(snapshot)
            return INBOUND_PROCESSED, "Oferta base llegó después de una actualización; orden incierto"

        snapshot.uncertain = True
        session.add(snapshot)
        return INBOUND_NOT_PROCESSED, "Oferta base repetida o fuera de orden"

    if payload.type == "offer-updated":
        if snapshot is None:
            session.add(MarketFeedOffer(
                cycle_id=payload.cycleId, city_id=payload.cityId, offer_id=offer_id,
                remaining=payload.data["remaining"],
                price_per_energy=Decimal(str(payload.data["pricePerEnergy"])),
                state="UNRESOLVED", uncertain=True,
                last_event_at=event_time, last_event_msg_id=str(payload.msgId),
            ))
            return INBOUND_NOT_PROCESSED, "Actualización recibida antes de la oferta base"

        if snapshot.state in {"WITHDRAWN", "CLOSED"} or event_time <= snapshot.last_event_at:
            snapshot.uncertain = True
            session.add(snapshot)
            return INBOUND_NOT_PROCESSED, "Actualización atrasada o posterior al retiro"

        snapshot.remaining = payload.data["remaining"]
        snapshot.price_per_energy = Decimal(str(payload.data["pricePerEnergy"]))
        snapshot.last_event_at = event_time
        snapshot.last_event_msg_id = str(payload.msgId)
        session.add(snapshot)
        return INBOUND_PROCESSED, None

    # offer-withdrawn es terminal para la oferta de este ciclo.
    if snapshot is None:
        session.add(MarketFeedOffer(
            cycle_id=payload.cycleId, city_id=payload.cityId, offer_id=offer_id,
            remaining=0, state="WITHDRAWN", uncertain=True,
            last_event_at=event_time, last_event_msg_id=str(payload.msgId),
        ))
        return INBOUND_PROCESSED, "Retiro recibido antes de la oferta base"

    if snapshot.state in {"WITHDRAWN", "CLOSED"} or event_time <= snapshot.last_event_at:
        snapshot.uncertain = True
        session.add(snapshot)
        return INBOUND_NOT_PROCESSED, "Retiro atrasado o repetido"

    snapshot.state = "WITHDRAWN"
    snapshot.remaining = 0
    snapshot.last_event_at = event_time
    snapshot.last_event_msg_id = str(payload.msgId)
    session.add(snapshot)
    return INBOUND_PROCESSED, None


def _apply_market_closed(
    session: Session, payload: MarketFeedMessageIn, cycle: MarketCycle
) -> tuple[str, str | None]:
    if cycle.closed_at is not None:
        return INBOUND_NOT_PROCESSED, "Cierre adicional para un mercado ya cerrado"

    cycle.closed_at = payload.timestamp
    cycle.closed_msg_id = str(payload.msgId)
    session.add(cycle)
    for snapshot in session.exec(select(MarketFeedOffer).where(
        MarketFeedOffer.cycle_id == payload.cycleId
    )).all():
        if snapshot.state in {"ACTIVE", "UNRESOLVED"}:
            snapshot.state = "CLOSED"
            session.add(snapshot)
    return INBOUND_PROCESSED, None


def persist_market_feed(session: Session, payload: MarketFeedMessageIn) -> InboundMessage:
    """Guarda recepción, claim y snapshot en una sola transacción PostgreSQL."""
    original = payload.model_dump(mode="json", exclude_none=True)
    sender = payload.cityId if payload.type != "market-closed" else "central"
    offer_id = payload.data.get("offerId")
    inbound = record_inbound_message(
        session, msg_id=str(payload.msgId), idpk=str(payload.idpk),
        message_type=payload.type, payload=original, cycle_id=payload.cycleId,
        sender=sender, offer_id=offer_id, commit=False,
    )

    fingerprint = _fingerprint(original)
    claim = session.execute(insert(MarketEventClaim).values(
        sender=sender, idpk=str(payload.idpk), msg_id=str(payload.msgId),
        fingerprint=fingerprint, inbound_id=inbound.id,
    ).on_conflict_do_nothing().returning(MarketEventClaim.id)).scalar_one_or_none()

    if claim is None:
        existing = session.exec(select(MarketEventClaim).where(
            or_(
                (MarketEventClaim.sender == sender) & (MarketEventClaim.idpk == str(payload.idpk)),
                (MarketEventClaim.sender == sender) & (MarketEventClaim.msg_id == str(payload.msgId)),
            )
        )).first()
        same_operation = (
            existing is not None and existing.idpk == str(payload.idpk)
            and existing.fingerprint == fingerprint
        )
        mark_inbound_result(
            session, inbound,
            status=INBOUND_DUPLICATE if same_operation else INBOUND_NOT_PROCESSED,
            reason="Redelivery de la misma operación" if same_operation else "idpk o msgId reutilizado con otro contenido",
            commit=False,
        )
        session.commit()
        session.refresh(inbound)
        return inbound

    cycle = _lock_market_cycle(session, payload.cycleId)
    if payload.type == "market-closed":
        status, reason = _apply_market_closed(session, payload, cycle)
    else:
        status, reason = _apply_offer_event(session, payload, cycle)

    mark_inbound_result(session, inbound, status=status, reason=reason, commit=False)
    session.commit()
    session.refresh(inbound)
    return inbound
