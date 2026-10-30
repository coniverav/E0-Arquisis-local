"""E2-065 / RF06: métricas persistidas y comparables sin escritura en GET."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func
from sqlmodel import Session, select

from ..database import get_session
from ..models import CycleStrategyMetric

router = APIRouter(prefix="/market", tags=["market-rf06"])


class CycleMetricOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    cycleId: str
    strategyVersion: int
    spanSeconds: int
    selectionPolicy: str
    acceptancePolicy: str
    deficitKwh: int
    boughtKwh: int
    coveredKwh: int
    coverageRate: Decimal
    rejectedBids: int
    rejectedIncomingBids: int
    buyCost: Decimal
    transportCost: Decimal
    settlementCost: Decimal | None
    realCost: Decimal | None
    baselineCost: Decimal | None
    baselineSource: str | None
    savingsVsBaseline: Decimal | None
    missingTransportTrades: int
    status: str
    createdAt: datetime
    updatedAt: datetime


class CycleMetricsCompareOut(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[CycleMetricOut]


def _view(metric: CycleStrategyMetric) -> CycleMetricOut:
    return CycleMetricOut(
        cycleId=metric.cycle_id,
        strategyVersion=metric.strategy_version,
        spanSeconds=metric.span_seconds,
        selectionPolicy=metric.selection_policy,
        acceptancePolicy=metric.acceptance_policy,
        deficitKwh=metric.deficit_kwh,
        boughtKwh=metric.bought_kwh,
        coveredKwh=metric.covered_kwh,
        coverageRate=metric.coverage_rate,
        rejectedBids=metric.rejected_bids,
        rejectedIncomingBids=metric.rejected_incoming_bids,
        buyCost=metric.buy_cost,
        transportCost=metric.transport_cost,
        settlementCost=metric.settlement_cost,
        realCost=metric.real_cost,
        baselineCost=metric.baseline_cost,
        baselineSource=metric.baseline_source,
        savingsVsBaseline=metric.savings_vs_baseline,
        missingTransportTrades=metric.missing_transport_trades,
        status=metric.status,
        createdAt=metric.created_at,
        updatedAt=metric.updated_at,
    )


@router.get("/cycle-metrics/compare", response_model=CycleMetricsCompareOut)
def compare_cycle_metrics(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = Query(default=None, pattern="^(IN_PROGRESS|AWAITING_SETTLEMENT|AWAITING_TRANSPORT|FINAL)$"),
    session: Session = Depends(get_session),
) -> CycleMetricsCompareOut:
    query = select(CycleStrategyMetric)
    count = select(func.count()).select_from(CycleStrategyMetric)
    if status is not None:
        query = query.where(CycleStrategyMetric.status == status)
        count = count.where(CycleStrategyMetric.status == status)
    total = session.exec(count).one()
    rows = session.exec(
        query.order_by(CycleStrategyMetric.created_at.desc(), CycleStrategyMetric.cycle_id)
        .offset(offset).limit(limit)
    ).all()
    return CycleMetricsCompareOut(total=total, offset=offset,
                                  limit=limit, items=[_view(row) for row in rows])


@router.get("/cycle-metrics", response_model=CycleMetricOut)
def get_cycle_metric(
    cycleId: str = Query(min_length=1, max_length=128),
    session: Session = Depends(get_session),
) -> CycleMetricOut:
    metric = session.get(CycleStrategyMetric, cycleId)
    if metric is None:
        raise HTTPException(status_code=404, detail="Cycle metrics not found")
    return _view(metric)
