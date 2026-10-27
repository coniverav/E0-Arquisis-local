"""Operaciones manuales autenticadas sobre ofertas seller (E2-029)."""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session

from app.auth import verify_jwt
from app.database import get_session
from app.services.seller_offer_events import change_local_offer_price, withdraw_local_offer

router = APIRouter(prefix="/market/offers", tags=["seller-offers"])


class RepriceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pricePerEnergy: Decimal = Field(gt=0)


@router.patch("/{offer_id}/price")
def reprice(
    offer_id: str, payload: RepriceIn,
    session: Session = Depends(get_session),
    _claims: dict = Depends(verify_jwt),
):
    try:
        changed = change_local_offer_price(
            session, offer_id=offer_id, price=payload.pricePerEnergy
        )
        session.commit()
        return {"offerId": offer_id, "changed": changed}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{offer_id}/withdraw")
def withdraw(
    offer_id: str,
    session: Session = Depends(get_session),
    _claims: dict = Depends(verify_jwt),
):
    try:
        changed = withdraw_local_offer(session, offer_id=offer_id)
        session.commit()
        return {"offerId": offer_id, "changed": changed}
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
