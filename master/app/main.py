from datetime import datetime, timedelta, timezone
import json
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, exists, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .config import CORS_ALLOWED_ORIGINS, INSTANCE_NAME
from .database import engine, get_session
from .models import Demand, Event, ProtocolError, Negotiation
from .schemas import (
    DemandPayload,
    EventOut,
    EventPayload,
    HistoryOut,
    PackageBodyPayload,
    ProtocolErrorOut,
    ProtocolErrorPayload,
)
from .routers.cycles import router as cycles_router
from .routers.internal_messages import router as internal_messages_router
from .routers.message_audit import (
    public_router as public_message_audit_router,
    router as message_audit_router,
)
from .routers.distance_tables import router as distance_tables_router
from .routers.connectivity import router as connectivity_router

from .services.negotiation_state import (
    NEGOTIATION_REJECTED,
    transition_negotiation,
)

app = FastAPI(
    title="EnergyShark E1",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cycles_router)
app.include_router(internal_messages_router)
app.include_router(message_audit_router)
app.include_router(public_message_audit_router)
app.include_router(distance_tables_router)
app.include_router(connectivity_router)

def _event_to_out(session: Session, event: Event) -> EventOut:
    demands = session.exec(
        select(Demand).where(Demand.event_id == event.id).order_by(Demand.id)
    ).all()

    return EventOut(
        id=event.id,
        idpk=UUID(event.idpk),
        type=event.type,
        packageBody=PackageBodyPayload(
            demands=[
                DemandPayload(city=d.city, demand=d.demand, unit=d.unit) for d in demands
            ],
            validUntil=event.valid_until,
            metaContent=event.meta_content,
            constraints=event.constraints,
        ),
        receivedAt=event.received_at,
    )

#Convierte un error persistido al schema de salida de la API.
def _protocol_error_to_out(error: ProtocolError) -> ProtocolErrorOut:
    return ProtocolErrorOut(
        id=error.id,
        idpk=UUID(error.idpk),
        msgId=UUID(error.msg_id),
        cycleId=error.cycle_id,
        reason=error.reason,
        code=error.code,
        target=UUID(error.target_msg_id),
        message=error.message,
        cap=error.cap,
        spare=error.spare,
        timestamp=error.timestamp,
        receivedAt=error.received_at,
    )


def _parse_time_filter(value: str, field_name: str):
    """Acepta fecha YYYY-MM-DD (día completo) o datetime ISO8601 exacto."""
    try:
        if len(value) == 10:
            start = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return "range", start, start + timedelta(days=1)

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return "exact", parsed.astimezone(timezone.utc), None
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} debe ser YYYY-MM-DD o un datetime ISO8601 con zona horaria",
        ) from exc


@app.get("/health")
def health() -> dict:
    """HEALTHCHECK real: verifica que el proceso y PostgreSQL respondan."""
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return {"status": "ok", "instance": INSTANCE_NAME}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc


@app.post("/internal/events", response_model=EventOut)
def ingest_event(payload: EventPayload, session: Session = Depends(get_session)) -> EventOut:
    """
    Endpoint usado SOLO por connector.

    idpk es único, por lo que un mensaje redelivered no genera duplicados: si ya existe,
    devolvemos el registro almacenado y connector puede hacer ACK con seguridad.
    """
    existing = session.exec(select(Event).where(Event.idpk == str(payload.idpk))).first()
    if existing:
        return _event_to_out(session, existing)

    body = payload.packageBody
    event = Event(
        idpk=str(payload.idpk),
        type=payload.type,
        valid_until=body.validUntil.astimezone(timezone.utc),
        meta_content=body.metaContent,
        constraints=body.constraints,
        received_at=datetime.now(timezone.utc),
    )

    try:
        session.add(event)
        session.flush()  # Obtiene event.id sin cerrar la transacción.

        for demand in body.demands:
            session.add(
                Demand(
                    event_id=event.id,
                    city=demand.city,
                    demand=demand.demand,
                    unit=demand.unit,
                )
            )

        session.commit()
        session.refresh(event)
    except IntegrityError:
        # Dos réplicas podrían recibir el mismo idpk casi simultáneamente.
        session.rollback()
        existing = session.exec(select(Event).where(Event.idpk == str(payload.idpk))).first()
        if not existing:
            raise
        event = existing

    return _event_to_out(session, event)

#Persiste un mensaje error recibido desde la central.
@app.post("/internal/protocol/errors", response_model=ProtocolErrorOut)
def ingest_protocol_error(
    payload: ProtocolErrorPayload,
    session: Session = Depends(get_session),
) -> ProtocolErrorOut:

    #msgId identifica al mensaje concreto y evita persistir una redelivery dos veces.
    existing = session.exec(
        select(ProtocolError).where(
            ProtocolError.msg_id == str(payload.msgId)
        )
    ).first()

    if existing:
        return _protocol_error_to_out(existing)

    error = ProtocolError(
        idpk=str(payload.idpk),
        msg_id=str(payload.msgId),
        cycle_id=payload.cycleId,
        reason=payload.reason,
        code=payload.code,
        target_msg_id=str(payload.data.target),
        message=payload.data.message,
        cap=payload.data.cap,
        spare=payload.data.spare,
        raw=payload.model_dump(mode="json"),
        timestamp=payload.timestamp.astimezone(timezone.utc),
        received_at=datetime.now(timezone.utc),
    )

    try:
        session.add(error)

        # Si el error responde a una negotiation-proposal,
        # cerramos esa negociación como REJECTED.
        negotiation = session.exec(
            select(Negotiation).where(
                Negotiation.latest_msg_id
                == str(payload.data.target)
            )
        ).first()

        if negotiation is not None:
            transition_negotiation(
                session,
                negotiation,
                NEGOTIATION_REJECTED,
                now=datetime.now(timezone.utc),
            )

        session.commit()
        session.refresh(error)

    except IntegrityError:
        #Dos réplicas podrían intentar persistir el mismo msgId simultáneamente.
        session.rollback()

        existing = session.exec(
            select(ProtocolError).where(
                ProtocolError.msg_id == str(payload.msgId)
            )
        ).first()

        if not existing:
            raise

        error = existing

    return _protocol_error_to_out(error)

@app.get("/history/{event_id}", response_model=EventOut)
def history_detail(event_id: int, session: Session = Depends(get_session)) -> EventOut:
    event = session.get(Event, event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_to_out(session, event)


@app.get("/history", response_model=HistoryOut)
def history(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    id: int | None = Query(None, ge=1),
    idpk: UUID | None = None,
    event_type: str | None = Query(None, alias="type"),
    receivedAt: str | None = None,
    validUntil: str | None = None,
    metaContent: str | None = None,
    constraints: str | None = None,
    city: str | None = None,
    demand: float | None = None,
    unit: str | None = None,
    session: Session = Depends(get_session),
) -> HistoryOut:
    """
    Historial paginado y filtrable.

    Filtros cubiertos: id, idpk, type, receivedAt, validUntil, metaContent,
    constraints (JSON exacto) y cada propiedad de demands: city, demand y unit.
    """
    conditions = []

    if id is not None:
        conditions.append(Event.id == id)
    if idpk is not None:
        conditions.append(Event.idpk == str(idpk))
    if event_type is not None:
        conditions.append(Event.type == event_type)
    if metaContent is not None:
        conditions.append(Event.meta_content == metaContent)

    if receivedAt is not None:
        mode, start, end = _parse_time_filter(receivedAt, "receivedAt")
        conditions.append(
            and_(Event.received_at >= start, Event.received_at < end)
            if mode == "range"
            else Event.received_at == start
        )

    if validUntil is not None:
        mode, start, end = _parse_time_filter(validUntil, "validUntil")
        conditions.append(
            and_(Event.valid_until >= start, Event.valid_until < end)
            if mode == "range"
            else Event.valid_until == start
        )

    if constraints is not None:
        try:
            parsed_constraints = json.loads(constraints)
            if not isinstance(parsed_constraints, dict):
                raise ValueError
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail="constraints debe ser un objeto JSON válido"
            ) from exc
        conditions.append(Event.constraints == parsed_constraints)

    # Los tres filtros de demanda, si se usan juntos, deben coincidir en la misma fila.
    if city is not None or demand is not None or unit is not None:
        demand_query = select(Demand.id).where(Demand.event_id == Event.id)
        if city is not None:
            demand_query = demand_query.where(Demand.city == city)
        if demand is not None:
            demand_query = demand_query.where(Demand.demand == demand)
        if unit is not None:
            demand_query = demand_query.where(Demand.unit == unit)
        conditions.append(exists(demand_query))

    statement = select(Event)
    count_statement = select(func.count(Event.id))
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    total = session.exec(count_statement).one()
    events = session.exec(
        statement.order_by(Event.received_at.desc(), Event.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return HistoryOut(
        page=page,
        limit=limit,
        total=total,
        items=[_event_to_out(session, event) for event in events],
    )

