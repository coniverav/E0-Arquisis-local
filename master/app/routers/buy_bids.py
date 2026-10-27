"""E2-038: entrada interna de encolado (el connector publica la outbox)."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ConfigDict
from sqlmodel import Session
from uuid import UUID

from ..database import get_session
from ..services.buy_bids import enqueue_buy_bid

router = APIRouter(prefix='/internal/market', tags=['internal-market'])


class BuyBidIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cycleId: str = Field(min_length=1, max_length=64)
    offerId: str = Field(min_length=1)
    quantity: int = Field(gt=0, strict=True)
    idpk: UUID


@router.post('/bids', status_code=201)
def create_buy_bid(payload: BuyBidIn, session: Session = Depends(get_session)):
    """Genera bid dirigido sin price; el ACK nunca marca aceptación comercial."""
    return enqueue_buy_bid(session, cycle_id=payload.cycleId,
                           offer_id=payload.offerId, quantity=payload.quantity,
                           idpk=payload.idpk)
