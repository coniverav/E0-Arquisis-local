from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.database import get_session
from app.services.saga_callbacks import complete_callback_delivery


router = APIRouter(prefix="/internal/saga-callbacks", tags=["internal"])


class CallbackResultIn(BaseModel):
    receipt: str = Field(min_length=64, max_length=64)
    status: Literal["SUCCEEDED", "FAILED"]
    callbackIdpk: str | None = None


@router.post("/result")
def record_callback_result(
    payload: CallbackResultIn,
    session: Session = Depends(get_session),
):
    token, changed = complete_callback_delivery(
        session,
        receipt=payload.receipt,
        status=payload.status,
        callback_idpk=payload.callbackIdpk,
    )
    if token is None:
        raise HTTPException(status_code=404, detail="Callback desconocido")
    session.commit()
    return {
        "status": token.status,
        "updated": changed,
    }
