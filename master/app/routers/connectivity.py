from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import DistanceTable
from ..schemas import ConnectivityItemOut, ConnectivityOut


router = APIRouter(
    tags=["connectivity"],
)


@router.get(
    "/connectivity",
    response_model=ConnectivityOut,
)
def get_connectivity(
    session: Session = Depends(get_session),
) -> ConnectivityOut:
    distance_table = session.exec(
        select(DistanceTable)
        .order_by(
            DistanceTable.source_timestamp.desc(),
            DistanceTable.received_at.desc(),
            DistanceTable.id.desc(),
        )
    ).first()

    if distance_table is None:
        raise HTTPException(
            status_code=404,
            detail="Distance table not found",
        )

    items = [
        ConnectivityItemOut(
            destination=destination,
            distance=values["distance"],
            transportCost=values["transportCost"],
            enabled=values["enabled"],
        )
        for destination, values in sorted(
            distance_table.distances.items()
        )
    ]

    return ConnectivityOut(
        timestamp=distance_table.source_timestamp,
        total=len(items),
        items=items,
    )
