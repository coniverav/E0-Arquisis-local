"""E2-057: lectura de mercado y swaps, administración autenticada de AD6.

El book y su precio efectivo ya se ofrecen en GET /market/book (E2-035/036).
Este router solo agrega las proyecciones que necesita el frontend E2-058/060.
"""
from decimal import Decimal
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import Session, select

from ..auth import verify_jwt
from ..database import get_session
from ..models import Cycle, MarketOffer, StrategyConfig, SwapRecord
from ..services.strategy_config import (
    BID_POLICIES, SELECTION_POLICIES, StrategyVersionConflict,
    create_pending_strategy, get_cycle_strategy,
)

router = APIRouter(prefix="/market", tags=["market-admin"])


class StrategyOut(BaseModel):
    version: int
    status: str
    offerPriceMarkup: Decimal
    offerQuantityFraction: Decimal
    bidAcceptancePolicy: str
    offerSelectionPolicy: str
    spanSeconds: int
    effectiveFromCycleId: str | None
    createdAt: datetime
    createdBy: str | None


class StrategyViewOut(BaseModel):
    active: StrategyOut | None
    pending: StrategyOut | None
    cycleStrategy: StrategyOut | None


class StrategyUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # PostgreSQL usa Numeric(10, 4): rechazar precisión que se redondearía
    # silenciosamente y no coincidiría con el valor auditado de la edición.
    offerPriceMarkup: Decimal = Field(gt=0, max_digits=10, decimal_places=4)
    offerQuantityFraction: Decimal = Field(ge=0, le=1, max_digits=10, decimal_places=4)
    bidAcceptancePolicy: str
    offerSelectionPolicy: str
    spanSeconds: int = Field(ge=0, strict=True)
    # Opcional para clientes anteriores. Evita ediciones sobre valores obsoletos.
    expectedVersion: int | None = Field(default=None, ge=1, strict=True)


class StrategyChangeOut(BaseModel):
    before: str | int | None
    after: str | int


class StrategyAuditItemOut(BaseModel):
    version: int
    previousVersion: int | None
    status: str
    createdAt: datetime
    createdBy: str | None
    effectiveFromCycleId: str | None
    changes: dict[str, StrategyChangeOut]


class StrategyAuditOut(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[StrategyAuditItemOut]


class StrategyOptionsOut(BaseModel):
    bidAcceptancePolicies: list[str]
    offerSelectionPolicies: list[str]
    minSpanSeconds: int
    decimalPlaces: int


class LocalOfferOut(BaseModel):
    offerId: str
    cycleId: str
    cityId: str
    initialQuantity: int
    remaining: int
    pricePerEnergy: Decimal
    status: str
    publishedAt: datetime
    lastUpdatedAt: datetime
    withdrawnAt: datetime | None


class LocalOffersOut(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[LocalOfferOut]


class SwapOut(BaseModel):
    # Nunca incluir swap_token ni idempotency_key: son credenciales de operación.
    id: int
    idpk: str
    swapId: str | None
    cycleId: str
    role: str
    counterpartCityId: str
    giveAsset: str
    giveAmount: Decimal
    takeAsset: str
    takeAmount: Decimal
    memo: str | None
    status: str
    rejectionReason: str | None
    sourceBidIdpk: str | None
    proposalHttpAcceptedAt: datetime | None
    proposalAttemptCount: int
    proposalLastError: str | None
    acceptAttempts: int
    acceptAttemptedAt: datetime | None
    acceptError: str | None
    expiresAt: datetime | None
    createdAt: datetime
    settledAt: datetime | None


class SwapsOut(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[SwapOut]


def _iso(dt):
    return dt


def _strategy(config: StrategyConfig | None) -> StrategyOut | None:
    if config is None:
        return None
    return StrategyOut(
        version=config.version, status=config.status,
        offerPriceMarkup=config.offer_price_markup,
        offerQuantityFraction=config.offer_quantity_fraction,
        bidAcceptancePolicy=config.bid_acceptance_policy,
        offerSelectionPolicy=config.offer_selection_policy,
        spanSeconds=config.span_seconds,
        effectiveFromCycleId=config.effective_from_cycle_id,
        createdAt=_iso(config.created_at), createdBy=config.created_by,
    )


def _swap(record: SwapRecord) -> SwapOut:
    return SwapOut(
        id=record.id, idpk=record.idpk, swapId=record.swap_id,
        cycleId=record.cycle_id, role=record.role,
        counterpartCityId=record.counterpart_city_id,
        giveAsset=record.give_asset, giveAmount=record.give_amount,
        takeAsset=record.take_asset, takeAmount=record.take_amount,
        memo=record.memo, status=record.status,
        rejectionReason=record.rejection_reason,
        sourceBidIdpk=record.source_bid_idpk,
        proposalHttpAcceptedAt=_iso(record.proposal_http_accepted_at),
        proposalAttemptCount=record.proposal_attempt_count,
        proposalLastError=record.proposal_last_error,
        acceptAttempts=record.accept_attempts,
        acceptAttemptedAt=_iso(record.accept_attempted_at),
        acceptError=record.accept_error,
        expiresAt=_iso(record.expires_at),
        createdAt=_iso(record.created_at), settledAt=_iso(record.settled_at),
    )


@router.get("/strategy", response_model=StrategyViewOut)
def read_strategy(
    cycleId: str | None = Query(default=None, min_length=1, max_length=64),
    session: Session = Depends(get_session),
) -> StrategyViewOut:
    """No crea una estrategia por el simple hecho de consultar (GET sin writes)."""
    if cycleId is not None and session.get(Cycle, cycleId) is None:
        raise HTTPException(status_code=404, detail="Cycle not found")
    active = session.exec(
        select(StrategyConfig).where(StrategyConfig.status == "ACTIVE")
        .order_by(StrategyConfig.version.desc())
    ).first()
    pending = session.exec(
        select(StrategyConfig).where(StrategyConfig.status == "PENDING")
        .order_by(StrategyConfig.version.desc())
    ).first()
    effective = get_cycle_strategy(session, cycleId) if cycleId else None
    return StrategyViewOut(active=_strategy(active), pending=_strategy(pending),
                           cycleStrategy=_strategy(effective))


@router.get("/strategies", response_model=list[StrategyOut])
def read_strategy_versions(
    session: Session = Depends(get_session),
) -> list[StrategyOut]:
    """Historial completo y ordenado de versiones de estrategia (AD6/RF06)."""
    versions = session.exec(
        select(StrategyConfig).order_by(StrategyConfig.version.desc())
    ).all()
    return [_strategy(version) for version in versions]


@router.get("/strategy/options", response_model=StrategyOptionsOut)
def read_strategy_options() -> StrategyOptionsOut:
    """Valores oficiales soportados por la API para un editor frontend."""
    return StrategyOptionsOut(
        bidAcceptancePolicies=sorted(BID_POLICIES),
        offerSelectionPolicies=sorted(SELECTION_POLICIES),
        minSpanSeconds=0, decimalPlaces=4,
    )


_STRATEGY_FIELDS = {
    "offerPriceMarkup": "offer_price_markup",
    "offerQuantityFraction": "offer_quantity_fraction",
    "bidAcceptancePolicy": "bid_acceptance_policy",
    "offerSelectionPolicy": "offer_selection_policy",
    "spanSeconds": "span_seconds",
}


def _strategy_audit_changes(
    current: StrategyConfig, previous: StrategyConfig | None,
) -> dict[str, StrategyChangeOut]:
    """Reconstruir cambios de versiones persistidas, sin actualizar ninguna fila."""
    changes = {}
    for public_name, column_name in _STRATEGY_FIELDS.items():
        after = getattr(current, column_name)
        before = getattr(previous, column_name) if previous is not None else None
        if before == after and previous is not None:
            continue
        # Conservar exactitud de los números de precisión fija.
        if isinstance(after, Decimal):
            after = str(after)
        if isinstance(before, Decimal):
            before = str(before)
        changes[public_name] = StrategyChangeOut(before=before, after=after)
    return changes


@router.get("/strategy/audit", response_model=StrategyAuditOut)
def read_strategy_audit(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
    _claims: dict = Depends(verify_jwt),
) -> StrategyAuditOut:
    """AD6: actor, versión, diferencias y primer ciclo efectivo. Solo lectura.

    strategy_configs es un historial versionado: los parámetros nunca se
    sobrescriben; únicamente evolucionan status y effective_from_cycle_id.
    """
    total = session.exec(select(func.count()).select_from(StrategyConfig)).one()
    versions = session.exec(
        select(StrategyConfig).order_by(StrategyConfig.version.desc())
        .offset(offset).limit(limit)
    ).all()
    previous_versions = [version.version - 1 for version in versions if version.version > 1]
    predecessors = (session.exec(
        select(StrategyConfig).where(StrategyConfig.version.in_(previous_versions))
    ).all() if previous_versions else [])
    preceding = {config.version: config for config in predecessors}
    return StrategyAuditOut(
        total=total, offset=offset, limit=limit,
        items=[StrategyAuditItemOut(
            version=current.version,
            previousVersion=(current.version - 1) if current.version > 1 else None,
            status=current.status, createdAt=current.created_at,
            createdBy=current.created_by,
            effectiveFromCycleId=current.effective_from_cycle_id,
            changes=_strategy_audit_changes(current, preceding.get(current.version - 1)),
        ) for current in versions],
    )


@router.post("/strategy", response_model=StrategyOut, status_code=201)
def update_strategy(
    payload: StrategyUpdateIn,
    session: Session = Depends(get_session),
    claims: dict = Depends(verify_jwt),
) -> StrategyOut:
    """Crea una versión pendiente. El ciclo activo mantiene su versión fija."""
    subject = claims.get("sub") if isinstance(claims, dict) else None
    if not isinstance(subject, str) or not subject.strip():
        raise HTTPException(status_code=403, detail="JWT sin identidad auditable")
    try:
        config = create_pending_strategy(
            session,
            offer_price_markup=payload.offerPriceMarkup,
            offer_quantity_fraction=payload.offerQuantityFraction,
            bid_acceptance_policy=payload.bidAcceptancePolicy,
            offer_selection_policy=payload.offerSelectionPolicy,
            span_seconds=payload.spanSeconds,
            created_by=subject,
            expected_version=payload.expectedVersion,
        )
        session.commit()
        session.refresh(config)
        return _strategy(config)
    except StrategyVersionConflict as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail={
            "code": "STALE_STRATEGY_VERSION", "expectedVersion": exc.expected,
            "currentVersion": exc.actual,
        }) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/offers", response_model=LocalOffersOut)
def read_local_offers(
    cycleId: str | None = Query(default=None, min_length=1, max_length=64),
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> LocalOffersOut:
    """Ofertas seller de nuestra ciudad, no reemplaza al libro del feed."""
    filters = [MarketOffer.is_local.is_(True)]
    if cycleId is not None:
        filters.append(MarketOffer.cycle_id == cycleId)
    total = session.exec(select(func.count()).select_from(MarketOffer).where(*filters)).one()
    offers = session.exec(
        select(MarketOffer).where(*filters)
        .order_by(MarketOffer.published_at.desc(), MarketOffer.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return LocalOffersOut(total=total, offset=offset, limit=limit, items=[LocalOfferOut(
        offerId=o.offer_id, cycleId=o.cycle_id, cityId=o.city_id,
        initialQuantity=o.initial_quantity, remaining=o.remaining_quantity,
        pricePerEnergy=o.price_per_energy, status=o.status,
        publishedAt=_iso(o.published_at), lastUpdatedAt=_iso(o.last_updated_at),
        withdrawnAt=_iso(o.withdrawn_at),
    ) for o in offers])


@router.get("/swaps", response_model=SwapsOut)
def read_swaps(
    cycleId: str | None = Query(default=None, min_length=1, max_length=64),
    status: str | None = Query(default=None, min_length=1, max_length=32),
    role: str | None = Query(default=None, pattern="^(PROPOSER|ACCEPTOR)$"),
    offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> SwapsOut:
    filters = []
    if cycleId is not None:
        filters.append(SwapRecord.cycle_id == cycleId)
    if status is not None:
        filters.append(SwapRecord.status == status)
    if role is not None:
        filters.append(SwapRecord.role == role)
    total = session.exec(select(func.count()).select_from(SwapRecord).where(*filters)).one()
    records = session.exec(
        select(SwapRecord).where(*filters)
        .order_by(SwapRecord.created_at.desc(), SwapRecord.id.desc())
        .offset(offset).limit(limit)
    ).all()
    return SwapsOut(total=total, offset=offset, limit=limit,
                    items=[_swap(record) for record in records])


@router.get("/swaps/{record_id}", response_model=SwapOut)
def read_swap(
    record_id: int,
    session: Session = Depends(get_session),
) -> SwapOut:
    record = session.get(SwapRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Swap not found")
    return _swap(record)
