"""E2-035: consulta del libro de mercado, sin side effects ni compras."""
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from ..database import get_session
from ..schemas import MarketBookOut, MarketBuyCandidatesOut
from ..services.market_book import get_market_book
from ..services.offer_selection import get_buy_candidates

router = APIRouter(prefix="/market", tags=["market"])


@router.get("/book", response_model=MarketBookOut)
def read_market_book(
    cycleId: str = Query(..., min_length=1, max_length=64),
    includeInactive: bool = Query(False),
    session: Session = Depends(get_session),
) -> MarketBookOut:
    """Por defecto, solo ofertas con remaining positivo y mercado abierto.

    includeInactive permite ver retiradas, cerradas e inciertas para auditoría.
    """
    return get_market_book(
        session, cycle_id=cycleId, include_inactive=includeInactive
    )


@router.get("/buy-candidates", response_model=MarketBuyCandidatesOut)
def read_buy_candidates(
    cycleId: str = Query(..., min_length=1, max_length=64),
    session: Session = Depends(get_session),
) -> MarketBuyCandidatesOut:
    """Solo lectura. Nunca encola bids ni opera fuera de la ventana oficial."""
    return get_buy_candidates(session, cycle_id=cycleId)
