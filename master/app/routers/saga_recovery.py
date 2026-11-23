"""Solo lectura de indicadores AD7; exponer únicamente en red interna."""
from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..database import get_session
from ..services.saga_recovery_health import recovery_health

router = APIRouter(prefix="/internal/saga-recovery", tags=["internal"])


@router.get("/health")
def saga_recovery_health(session: Session = Depends(get_session)) -> dict:
    return recovery_health(session)
