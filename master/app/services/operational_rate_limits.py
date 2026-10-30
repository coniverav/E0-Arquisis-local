"""Cupos por ciudad compartidos por los procesos mediante PostgreSQL.

La reserva se confirma antes de la llamada externa: ante una respuesta perdida
se cuenta un intento, pero jamás se excede el presupuesto por réplicas locales.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from app.config import CITY_ID
from app.models import Cycle, OperationalRateLimit

DEFAULT_LIMITS = {
    "wallet:read": 300,
    "wallet:write": 120,
    "market:offer": 60,
    "central:request": 10,
}
LIMIT_FIELD = {
    "wallet:read": "walletReadsPerMinute",
    "wallet:write": "walletWritesPerMinute",
    "market:offer": "marketOpsPerMinute",
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("el instante requiere zona horaria")
    return value.astimezone(timezone.utc)


def _limit(session: Session, category: str) -> int:
    default = DEFAULT_LIMITS[category]
    field = LIMIT_FIELD.get(category)
    if field is None:
        return default
    cycle = session.exec(select(Cycle).where(
        Cycle.status_idpk.is_not(None),
    ).order_by(Cycle.market_opens_at.desc().nulls_last()).limit(1)).first()
    market = (cycle.status_payload or {}).get("market") if cycle else None
    limits = market.get("limits") if isinstance(market, dict) else None
    value = limits.get(field) if isinstance(limits, dict) else None
    # El enunciado fija los máximos por ciudad. Un anuncio menor prevalece.
    return min(default, value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


def _locked_bucket(session: Session, key: str) -> OperationalRateLimit:
    session.exec(insert(OperationalRateLimit.__table__).values(
        key=key, attempts=[], blocked_until=None,
    ).on_conflict_do_nothing(index_elements=["key"]))
    return session.exec(select(OperationalRateLimit).where(
        OperationalRateLimit.key == key
    ).with_for_update()).one()


def reserve(session: Session, category: str, *, now: datetime | None = None) -> int:
    """Devuelve 0 al reservar; si se difiere, segundos enteros de espera."""
    if category not in DEFAULT_LIMITS:
        raise ValueError("categoría de límite desconocida")
    current = _utc(now or datetime.now(timezone.utc))
    bucket = _locked_bucket(session, f"{CITY_ID}:{category}")
    if bucket.blocked_until is not None and _utc(bucket.blocked_until) > current:
        return max(1, ceil((_utc(bucket.blocked_until) - current).total_seconds()))
    recent = sorted(
        _utc(datetime.fromisoformat(value)) for value in bucket.attempts
        if _utc(datetime.fromisoformat(value)) > current - timedelta(seconds=60)
    )
    if len(recent) >= _limit(session, category):
        return max(1, ceil((recent[0] + timedelta(seconds=60) - current).total_seconds()))
    bucket.attempts = [value.isoformat() for value in recent] + [current.isoformat()]
    session.add(bucket)
    session.flush()
    return 0


def block(session: Session, category: str, seconds: int, *, now: datetime | None = None) -> None:
    """Propaga un Retry-After de Wallet a todas las instancias de la ciudad."""
    if category not in DEFAULT_LIMITS or seconds < 0:
        raise ValueError("bloqueo de límite inválido")
    current = _utc(now or datetime.now(timezone.utc))
    bucket = _locked_bucket(session, f"{CITY_ID}:{category}")
    deadline = current + timedelta(seconds=seconds)
    if bucket.blocked_until is None or _utc(bucket.blocked_until) < deadline:
        bucket.blocked_until = deadline
        session.add(bucket)
