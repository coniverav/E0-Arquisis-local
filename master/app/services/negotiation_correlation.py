"""Resuelve todos los intentos persistidos de una operación, no solo su último msgId."""
from sqlmodel import Session, select
from ..models import InboundMessage, LedgerEntry, Negotiation, OutboundMessage


def proposal_negotiation(session: Session, target: str) -> Negotiation | None:
    outbound = session.exec(select(OutboundMessage).where(
        OutboundMessage.msg_id == target,
        OutboundMessage.message_type == "negotiation-proposal",
    )).first()
    condition = (Negotiation.idpk == outbound.idpk if outbound is not None
                 else Negotiation.latest_msg_id == target)
    return session.exec(select(Negotiation).where(condition).with_for_update()).first()


def confirmation_negotiation(session: Session, target: str) -> Negotiation | None:
    effect = session.exec(select(LedgerEntry).where(
        LedgerEntry.source_msg_id == target,
        LedgerEntry.operation_type.in_(["GIVE_CONFIRMED", "TAKE_CONFIRMED"]),
    )).first()
    if effect is not None:
        return session.exec(select(Negotiation).where(Negotiation.id == effect.negotiation_id).with_for_update()).first()
    #Una reconfirmación puede tener nuevos ids sin duplicar el efecto contable.
    inbound = session.exec(select(InboundMessage).where(
        InboundMessage.msg_id == target, InboundMessage.message_type.in_(["give", "take"]),
        InboundMessage.status.in_(["PROCESSED", "DUPLICATE"]),
    )).first()
    if inbound is not None:
        proposal = (inbound.payload or {}).get("data", {}).get("target")
        if proposal:
            found = proposal_negotiation(session, proposal)
            if found is not None:
                return found
    return session.exec(select(Negotiation).where(Negotiation.latest_msg_id == target).with_for_update()).first()


def stop_proposal_dispatches(session: Session, negotiation: Negotiation) -> None:
    for outbound in session.exec(select(OutboundMessage).where(
        OutboundMessage.idpk == negotiation.idpk,
        OutboundMessage.message_type == "negotiation-proposal",
        OutboundMessage.dispatch_required.is_(True),
    )).all():
        outbound.dispatch_required = False
