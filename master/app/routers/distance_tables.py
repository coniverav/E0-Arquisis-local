from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..database import get_session
from ..models import DistanceTable
from ..schemas import DistanceTableOut


router = APIRouter(
    tags=["distance-table"],
)


@router.get(
    "/distance-table",
    response_model=DistanceTableOut,
)
def get_current_distance_table(
    session: Session = Depends(get_session),
) -> DistanceTableOut:

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

    return DistanceTableOut(
        id=distance_table.id,
        msgId=distance_table.msg_id,
        idpk=distance_table.idpk,
        timestamp=distance_table.source_timestamp,
        receivedAt=distance_table.received_at,
        distances=distance_table.distances,
    )