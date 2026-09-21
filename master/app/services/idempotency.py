from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session
from ..models import ProcessedIdpk

#Indica de forma determinista si un idpk ya fue procesado.
def is_idpk_processed(
    session: Session,
    idpk: str,
) -> bool:

    return session.get(ProcessedIdpk, idpk) is not None

#Intenta reclamar un idpk para procesamiento.
#Retorna True si esta transacción lo obtuvo.
#Retorna False si otra operación ya lo había reclamado.
#No hace commit, ya que el registro queda en la misma transacción que el efecto de negocio.
def claim_idpk(
    session: Session,
    *,
    idpk: str,
    msg_id: str,
    message_type: str,
    cycle_id: str | None,
) -> bool:
    statement = (
        insert(ProcessedIdpk)
        .values(
            idpk=idpk,
            msg_id=msg_id,
            message_type=message_type,
            cycle_id=cycle_id,
            processed_at=datetime.now(timezone.utc),
        )
        .on_conflict_do_nothing(
            index_elements=["idpk"],
        )
        .returning(ProcessedIdpk.idpk)
    )

    result = session.execute(statement)

    return result.scalar_one_or_none() is not None