from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..auth import verify_jwt
from ..database import get_session
from ..models import Cycle, Negotiation
from ..schemas import NegotiationCreate, NegotiationOut


router = APIRouter(
    prefix="/negotiations",
    tags=["negotiations"],
)


def _negotiation_to_out(
    negotiation: Negotiation,
) -> NegotiationOut:
    return NegotiationOut(
        id=negotiation.id,
        idpk=negotiation.idpk,
        direction=negotiation.direction,
        requestedQuantity=negotiation.requested_quantity,
        offeredPrice=negotiation.offered_price,
        status=negotiation.status,
        confirmedEnergy=negotiation.confirmed_energy,
        confirmedPrice=negotiation.confirmed_price,
        paymentQuantity=negotiation.payment_quantity,
        deadlineAt=negotiation.deadline_at,
    )


@router.post("", response_model=NegotiationOut, status_code=201)
def create_negotiation(
    payload: NegotiationCreate,
    response: Response,
    token: dict = Depends(verify_jwt),
    session: Session = Depends(get_session),
) -> NegotiationOut:
    """
    Crear una negociación manual en estado PROPOSED.

    Requiere JWT válido. La idempotencia es por idpk: un reintento
    con el mismo idpk devuelve la negociación existente con 200.
    """
    now = datetime.now(timezone.utc)

    cycle = session.get(Cycle, payload.cycleId)
    if cycle is None:
        raise HTTPException(status_code=404, detail="Cycle not found")

    existing = session.exec(
        select(Negotiation).where(Negotiation.idpk == str(payload.idpk))
    ).first()
    if existing is not None:
        response.status_code = 200
        return _negotiation_to_out(existing)

    negotiation = Negotiation(
        cycle_id=payload.cycleId,
        idpk=str(payload.idpk),
        latest_msg_id=None,
        direction=payload.direction,
        requested_quantity=payload.requestedQuantity,
        offered_price=payload.offeredPrice,
        status="PROPOSED",
        deadline_at=now + timedelta(seconds=30),
        created_at=now,
        updated_at=now,
    )

    try:
        session.add(negotiation)
        session.commit()
        session.refresh(negotiation)
    except IntegrityError:
        # Dos réplicas podrían crear el mismo idpk casi al mismo tiempo
        session.rollback()
        existing = session.exec(
            select(Negotiation).where(Negotiation.idpk == str(payload.idpk))
        ).first()
        if existing is None:
            raise
        response.status_code = 200
        return _negotiation_to_out(existing)

    # E1-43 conectar el publish. latest_msg_id se setea ahí.

    return _negotiation_to_out(negotiation)
