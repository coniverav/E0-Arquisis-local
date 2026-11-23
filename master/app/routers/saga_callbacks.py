"""API interna para relay de callbacks Step Functions; jamás exponer públicamente."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..database import get_session
from ..services.buyer_callback_dispatch import claim_buyer_callbacks, finish_buyer_callback

router = APIRouter(prefix="/internal/saga-callbacks", tags=["internal"])


class CallbackClaimIn(BaseModel):
    limit: int = Field(default=20, ge=1, le=100)
    leaseSeconds: int = Field(default=15, ge=1, le=60)


class CallbackResultIn(BaseModel):
    claimToken: str = Field(min_length=1, max_length=128)
    outcome: Literal["SUCCEEDED", "RETRY", "AMBIGUOUS"]
    retrySeconds: int = Field(default=2, ge=1, le=60)


@router.post("/claims")
def claim_callbacks(payload: CallbackClaimIn, session: Session = Depends(get_session)):
    rows = claim_buyer_callbacks(session, limit=payload.limit, lease_seconds=payload.leaseSeconds)
    session.commit()
    # Este recurso está reservado a la red interna de Connector, como outbox-relay.
    # Los tokens no deben aparecer en logs ni endpoints de auditoría públicos.
    return {"items": rows}


@router.post("/{token_id}/result")
def finish_callback(token_id: int, payload: CallbackResultIn,
                    session: Session = Depends(get_session)):
    changed = finish_buyer_callback(
        session, token_id=token_id, claim_token=payload.claimToken,
        outcome=payload.outcome, retry_seconds=payload.retrySeconds,
    )
    if not changed:
        session.rollback()
        raise HTTPException(status_code=409, detail="STALE_CALLBACK_CLAIM")
    session.commit()
    return {"updated": True, "status": payload.outcome}
