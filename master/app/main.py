from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import cast, Date

from . import models, schemas
from .database import engine, get_db, Base

# Crea las tablas si no existen (para esta entrega no usamos migraciones, basta con esto)
Base.metadata.create_all(bind=engine)

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Entrega 0 - EnergyShark - Constanza Vera"}

@app.get("/health")
def health():
    """Usado por el HEALTHCHECK de Docker para saber si el servicio está vivo."""
    return {"status": "ok"}


@app.post("/events", response_model=schemas.EventOut, status_code=201)
def create_event(event: schemas.EventIn, db: Session = Depends(get_db)):
    """
    El connector llama a este endpoint cada vez que consume un mensaje
    de RabbitMQ. Aquí se guarda en la base de datos.
    """
    # Si el idpk ya existe, no lo duplicamos (evita reprocesar el mismo evento dos veces)
    existing = db.query(models.DemandEvent).filter(
        models.DemandEvent.idpk == event.idpk
    ).first()
    if existing:
        return existing

    valid_until_raw = event.packageBody.get("validUntil")
    valid_until = None
    if valid_until_raw:
        valid_until = datetime.fromisoformat(valid_until_raw.replace("Z", "+00:00"))

    received_at = event.receivedAt or datetime.now(timezone.utc)

    db_event = models.DemandEvent(
        idpk=event.idpk,
        type=event.type,
        package_body=event.packageBody,
        valid_until=valid_until,
        received_at=received_at,
    )
    db.add(db_event)
    db.commit()
    db.refresh(db_event)
    return db_event


@app.get("/history", response_model=list[schemas.EventOut])
def list_history(
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=500),
    idpk: Optional[str] = None,
    type: Optional[str] = None,
    receivedAt: Optional[str] = None,   # formato YYYY-MM-DD
    validUntil: Optional[str] = None,   # formato YYYY-MM-DD
    city: Optional[str] = None,         # busca dentro de packageBody.demands
    db: Session = Depends(get_db),
):
    """Paginación de eventos históricos, con filtros opcionales."""
    q = db.query(models.DemandEvent)

    if idpk:
        q = q.filter(models.DemandEvent.idpk == idpk)
    if type:
        q = q.filter(models.DemandEvent.type == type)
    if receivedAt:
        day = datetime.strptime(receivedAt, "%Y-%m-%d").date()
        q = q.filter(cast(models.DemandEvent.received_at, Date) == day)
    if validUntil:
       day = datetime.strptime(validUntil, "%Y-%m-%d").date()
       q = q.filter(cast(models.DemandEvent.valid_until, Date) == day)
    if city:
        # packageBody.demands es una lista de objetos {city, demand, unit}
        # esto filtra los eventos que mencionan esa ciudad
        q = q.filter(
            models.DemandEvent.package_body["demands"].astext.contains(city)
        )

    q = q.order_by(models.DemandEvent.received_at.desc())
    offset = (page - 1) * limit
    return q.offset(offset).limit(limit).all()


@app.get("/history/{event_id}", response_model=schemas.EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    """RF2: detalle de un registro por el id que nosotros generamos."""
    event = db.query(models.DemandEvent).filter(
        models.DemandEvent.id == event_id
    ).first()
    if not event:
        raise HTTPException(status_code=404, detail="Evento no encontrado")
    return event
