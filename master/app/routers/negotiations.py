from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..config import CITY_ID, RABBITMQ_CENTRAL_ROUTING_KEY
from ..services.budget_carryover import lock_city_ledger
from ..services.negotiation_proposals import enqueue_negotiation_proposal
from ..auth import verify_jwt
from ..database import get_session
from ..models import Cycle, Negotiation
from ..schemas import (
    NegotiationCreate,
    NegotiationOut,
    NegotiationsOut,
)


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
        cycleId=negotiation.cycle_id,
        direction=negotiation.direction,
        requestedQuantity=negotiation.requested_quantity,
        offeredPrice=negotiation.offered_price,
        status=negotiation.status,
        confirmedEnergy=negotiation.confirmed_energy,
        confirmedPrice=negotiation.confirmed_price,
        paymentQuantity=negotiation.payment_quantity,
        deadlineAt=negotiation.deadline_at,
        createdAt=negotiation.created_at,
        updatedAt=negotiation.updated_at,
    )

@router.get("", response_model=NegotiationsOut)
def list_negotiations(
    token: dict = Depends(verify_jwt),
    session: Session = Depends(get_session),
) -> NegotiationsOut:

    negotiations = session.exec(
        select(Negotiation)
        .order_by(
            Negotiation.created_at.desc()
        )
    ).all()

    return NegotiationsOut(
        total=len(negotiations),
        items=[
            _negotiation_to_out(negotiation)
            for negotiation in negotiations
        ],
    )

@router.get(
    "/{negotiation_id}",
    response_model=NegotiationOut,
)
def get_negotiation(
    negotiation_id: int,
    token: dict = Depends(verify_jwt),
    session: Session = Depends(get_session),
) -> NegotiationOut:

    negotiation = session.get(
        Negotiation,
        negotiation_id,
    )

    if negotiation is None:
        raise HTTPException(
            status_code=404,
            detail="Negotiation not found",
        )

    return _negotiation_to_out(negotiation)

@router.post("", response_model=NegotiationOut, status_code=201)
def create_negotiation(
    payload: NegotiationCreate,
    response: Response,
    token: dict = Depends(verify_jwt),
    session: Session = Depends(get_session),
) -> NegotiationOut:
    """
    Crea una negociación voluntaria y deja su
    negotiation-proposal pendiente de publicación.

    Requiere JWT válido. La idempotencia es por idpk:
    un reintento con el mismo idpk devuelve la
    negociación existente con 200.
    """

    lock_city_ledger(session)

    #Mantener el 404 explícito de la API.
    cycle = session.get(Cycle, payload.cycleId)

    if cycle is None:
        raise HTTPException(
            status_code=404,
            detail="Cycle not found",
        )

    #Retry HTTP con el mismo idpk, no crear una segunda propuesta.
    existing = session.exec(
        select(Negotiation).where(
            Negotiation.idpk == str(payload.idpk)
        )
    ).first()

    if existing is not None:
        response.status_code = 200
        return _negotiation_to_out(existing)

    try:
        negotiation = enqueue_negotiation_proposal(
            session,
            cycle_id=payload.cycleId,
            direction=payload.direction,
            quantity=payload.requestedQuantity,
            price_per_energy=payload.offeredPrice,
            city_id=CITY_ID,
            routing_key=RABBITMQ_CENTRAL_ROUTING_KEY,
            idpk=str(payload.idpk),
        )

    except IntegrityError:
        #Dos réplicas podrían intentar crear el mismo idpk al mismo tiempo.
        session.rollback()

        existing = session.exec(
            select(Negotiation).where(
                Negotiation.idpk == str(payload.idpk)
            )
        ).first()

        if existing is None:
            raise

        response.status_code = 200
        return _negotiation_to_out(existing)

    except ValueError as exc:
        session.rollback()

        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc

    return _negotiation_to_out(negotiation)
