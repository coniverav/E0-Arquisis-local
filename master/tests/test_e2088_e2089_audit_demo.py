"""E2-088/089: correlacion consultable en auditoria; filas sintéticas locales.

Complementa E2-046 (efecto economico real en PostgreSQL de prueba) y las
pruebas del connector (NACK/DISCARD) sin fingir que estas filas fueron AMQP real.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import InboundMessage, LedgerEntry, MarketBid, ProcessedIdpk, SwapRecord

client = TestClient(app)


def _get_operator(path: str, params: dict):
    app.dependency_overrides[verify_jwt] = lambda: {"sub": "qa-e2088-e2089"}
    try:
        return client.get(path, params=params)
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def test_e2088_replay_mismo_idpk_auditado_y_correlacionado():
    """Auditoria de dos recepciones, mismo idpk, msgId nuevo: enlace visible.

    No prueba por si solo la deduplicacion de negocio; la cubre E2-046.
    """
    now = datetime.now(timezone.utc)
    cycle_id = f"qa-88-{uuid4()}"
    idpk = str(uuid4())
    first_msg = str(uuid4())
    retry_msg = str(uuid4())
    swap_id = f"swap-qa-{uuid4()}"
    body = {"data": {"swapId": swap_id}}

    with Session(engine) as session:
        session.add(ProcessedIdpk(
            idpk=idpk, msg_id=first_msg, message_type="swap-settled",
            cycle_id=cycle_id, processed_at=now,
        ))
        session.add_all([
            InboundMessage(
                msg_id=first_msg, idpk=idpk, message_type="swap-settled",
                cycle_id=cycle_id, sender="central", status="PROCESSED",
                payload=body, received_at=now,
            ),
            InboundMessage(
                msg_id=retry_msg, idpk=idpk, message_type="swap-settled",
                cycle_id=cycle_id, sender="central", status="DUPLICATE",
                reason="idpk already processed", payload=body,
                received_at=now + timedelta(seconds=1),
            ),
        ])
        session.commit()

    response = client.get("/audit/anomalies", params={
        "cycleId": cycle_id, "status": "DUPLICATE", "idpk": idpk,
    })
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert response.json()["total"] == 1
    assert items[0]["idpk"] == idpk
    assert items[0]["msgId"] == retry_msg
    assert items[0]["relatedMsgId"] == first_msg
    assert items[0]["status"] == "DUPLICATE"
    assert "payload" not in items[0] and "rawPayload" not in items[0]

    wallet = _get_operator("/audit/wallet-events", {"swapId": swap_id, "idpk": idpk})
    assert wallet.status_code == 200, wallet.text
    assert wallet.json()["total"] == 2
    assert {row["status"] for row in wallet.json()["items"]} == {"PROCESSED", "DUPLICATE"}
    assert {row["msgId"] for row in wallet.json()["items"]} == {first_msg, retry_msg}
    assert {row["idpk"] for row in wallet.json()["items"]} == {idpk}

    # Insertar evidencia no introduce, por si solo, efectos economicos.
    with Session(engine) as session:
        assert not session.exec(select(SwapRecord).where(SwapRecord.cycle_id == cycle_id)).all()
        assert not session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)).all()
    print("E2-088: auditoria DUPLICATE visible; mismo idpk, msgIds distintos, relatedMsgId correcto")


def test_e2089_nack_y_discard_visibles_sin_efectos_economicos():
    """Proyecciones HTTP de la auditoria de errores sinteticos de connector.

    El envio de NACK y ACK/reject del broker se comprueba aparte en
    connector/tests/test_city_identity.py y test_directed_ack_nack.py.
    """
    cycle_id = f"qa-89-{uuid4()}"
    malformed_msg = str(uuid4())
    identity_msg = str(uuid4())
    cases = [
        {"status": "NACKED", "msgId": malformed_msg, "idpk": str(uuid4()),
         "type": "bid", "cycleId": cycle_id, "sender": "COR",
         "reasonCode": "MALFORMED_MESSAGE", "reason": "bid sin quantity",
         "payload": {"type": "bid", "data": {"offerId": "offer-qa"}}},
        {"status": "NACKED", "msgId": identity_msg, "idpk": str(uuid4()),
         "type": "bid", "cycleId": cycle_id, "sender": "HGW",
         "reasonCode": "IDENTITY_MISMATCH", "reason": "user_id != cityId",
         "payload": {"type": "bid", "cityId": "COR", "data": {"quantity": 5}}},
        {"status": "DISCARDED", "type": "bid", "cycleId": cycle_id,
         "rawPayload": "no-es-json", "reason": "El mensaje no es JSON valido"},
    ]
    for case in cases:
        response = client.post("/internal/audit/inbound", json=case)
        assert response.status_code == 200, response.text

    response = client.get("/audit/anomalies", params={"cycleId": cycle_id})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert response.json()["total"] == 3
    assert {item["status"] for item in items} == {"NACKED", "DISCARDED"}
    assert {item["reasonCode"] for item in items if item["reasonCode"]} == {
        "MALFORMED_MESSAGE", "IDENTITY_MISMATCH",
    }
    assert all("payload" not in item and "rawPayload" not in item for item in items)
    assert "no-es-json" not in response.text

    identity = client.get("/audit/anomalies", params={
        "cycleId": cycle_id, "reasonCode": "IDENTITY_MISMATCH",
    })
    assert identity.status_code == 200 and identity.json()["total"] == 1
    assert identity.json()["items"][0]["msgId"] == identity_msg
    assert identity.json()["items"][0]["sender"] == "HGW"
    assert identity.json()["items"][0]["status"] == "NACKED"

    intercity = _get_operator("/audit/intercity", {"cycleId": cycle_id})
    assert intercity.status_code == 200, intercity.text
    # Solamente los dos bids correlacionables forman parte de la vista intercity.
    assert intercity.json()["total"] == 3
    assert {item["reasonCode"] for item in intercity.json()["items"] if item["reasonCode"]} == {
        "MALFORMED_MESSAGE", "IDENTITY_MISMATCH",
    }
    assert all("payload" not in item for item in intercity.json()["items"])

    with Session(engine) as session:
        assert not session.exec(select(MarketBid).where(MarketBid.cycle_id == cycle_id)).all()
        assert not session.exec(select(SwapRecord).where(SwapRecord.cycle_id == cycle_id)).all()
        assert not session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)).all()
    print("E2-089: NACKED MALFORMED/IDENTITY_MISMATCH y DISCARD consultables; sin bid/swap/ledger")
