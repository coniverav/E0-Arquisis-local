"""RF05: auditoría consultable y sin credenciales de mensajes y Wallet."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.auth import verify_jwt
from app.database import engine
from app.main import app
from app.models import (
    Cycle, InboundMessage, LedgerEntry, OutboundMessage, SwapRecord,
    WalletBalanceSnapshot, WalletReconciliation, WalletStatementEntry,
)


client = TestClient(app)


def _get(path, params=None):
    app.dependency_overrides[verify_jwt] = lambda: {"sub": "audit-operator"}
    try:
        return client.get(path, params=params)
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def test_new_audit_views_require_authentication():
    for path in ("/audit/intercity", "/audit/wallet-events", "/audit/reconciliations"):
        assert client.get(path).status_code == 401
        assert _get(path).json() == {"total": 0, "offset": 0, "limit": 50, "items": []}
    app.dependency_overrides[verify_jwt] = lambda: {"aud": "app", "sub": " "}
    try:
        assert client.get("/audit/wallet-events").status_code == 403
    finally:
        app.dependency_overrides.pop(verify_jwt, None)


def test_intercity_filters_correlation_and_pagination_without_payload():
    now = datetime.now(timezone.utc)
    cycle_id = f"audit-{uuid4()}"
    idpk = str(uuid4())
    incoming_msg = str(uuid4())
    outgoing_msg = str(uuid4())
    with Session(engine) as session:
        session.add(InboundMessage(
            msg_id=incoming_msg, idpk=idpk, message_type="bid", cycle_id=cycle_id,
            sender="HGW", status="NACKED", reason_code="IDENTITY_MISMATCH",
            payload={"data": {"secret": "private-value"}}, received_at=now,
        ))
        session.add(OutboundMessage(
            msg_id=outgoing_msg, idpk=str(uuid4()), message_type="nack",
            cycle_id=cycle_id, target_msg_id=incoming_msg,
            routing_key="city.HGW", status="PUBLISHED", created_at=now,
            payload={"data": {"token": "private-value"}},
        ))
        session.commit()
    response = _get("/audit/intercity", {"cycleId": cycle_id, "cityId": "HGW"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert {item["direction"] for item in body["items"]} == {"INBOUND", "OUTBOUND"}
    assert all("payload" not in item for item in body["items"])
    assert "private-value" not in response.text
    assert _get("/audit/intercity", {"msgId": incoming_msg}).json()["total"] == 2
    assert _get("/audit/intercity", {"reasonCode": "IDENTITY_MISMATCH"}).json()["total"] == 1
    page = _get("/audit/intercity", {"cycleId": cycle_id, "offset": 1, "limit": 1}).json()
    assert page["total"] == 2 and len(page["items"]) == 1


def test_wallet_event_and_reconciliation_expose_links_without_tokens():
    now = datetime.now(timezone.utc)
    cycle_id = f"audit-{uuid4()}"
    swap_id = f"swap-{uuid4()}"
    event_idpk = str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(cycle_id=cycle_id, created_at=now))
        session.add(SwapRecord(
            idpk=str(uuid4()), swap_id=swap_id, cycle_id=cycle_id,
            role="PROPOSER", counterpart_city_id="HGW", give_asset="KWH",
            give_amount=Decimal("2"), take_asset="CR", take_amount=Decimal("10"),
            status="SETTLED", idempotency_key="private-idempotency-key",
            created_at=now,
        ))
        session.add(InboundMessage(
            msg_id=str(uuid4()), idpk=event_idpk, message_type="swap-settled",
            status="PROCESSED", sender="central", received_at=now,
            payload={"data": {"swapId": swap_id, "token": "private-wallet-jwt",
                              "idpk": "private-wallet-key"}},
        ))
        session.add(LedgerEntry(
            cycle_id=cycle_id, sequence_no=1, operation_type="SWAP_SETTLED",
            idpk=swap_id, budget_delta=Decimal("10"), energy_delta=Decimal("-2"),
            budget_after=Decimal("10"), energy_after=Decimal("0"),
            applied_at=now,
        ))
        session.add(WalletStatementEntry(
            entry_id="entry-75", cycle_id=cycle_id, asset="CR", kind="swap",
            amount=Decimal("10"), reference=swap_id,
            created_at=now, fetched_at=now,
        ))
        session.add(WalletBalanceSnapshot(
            cycle_id=cycle_id, source_swap_id=swap_id, wallet_as_of=now,
            source="EVENT", recorded_at=now,
        ))
        session.add(WalletReconciliation(
            cycle_id=cycle_id, status="MISMATCH", cr_available_wallet=Decimal("10"),
            kwh_available_wallet=Decimal("0"), created_at=now,
            explanation={"wallet_as_of": now.isoformat(), "statement_complete": True,
                         "local_evidence": {"operations": [{
                             "operation": "SWAP_SETTLED", "reference": "private-reference",
                             "wallet_entry_ids": ["entry-75"], "ledger_entry_ids": [1],
                             "wallet_amount": "10", "local_amount": "9", "status": "MISMATCH",
                             "secret": "private-extra",
                         }]}},
        ))
        session.commit()
    response = _get("/audit/wallet-events", {"cycleId": cycle_id, "swapId": swap_id})
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["swapId"] == swap_id and item["cycleId"] == cycle_id
    assert item["swapStatus"] == "SETTLED"
    assert item["ledgerEntryIds"] and item["statementEntryIds"] == ["entry-75"]
    assert item["balanceSnapshotIds"] and item["reconciliationStatus"] == "MISMATCH"
    assert "private" not in response.text
    result = _get("/audit/reconciliations", {"cycleId": cycle_id, "status": "MISMATCH"})
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["operations"][0]["wallet_entry_ids"] == ["entry-75"]
    assert "private" not in result.text


def test_internal_audit_response_redacts_wallet_token_recursively():
    msg_id = str(uuid4())
    response = client.post("/internal/audit/inbound", json={
        "status": "DISCARDED", "reason": "Identidad no válida",
        "msgId": msg_id, "idpk": str(uuid4()),
        "type": "swap-proposed", "sender": "central",
        "payload": {"data": {"swapId": "swap-75", "token": "private-jwt",
                             "nested": [{"apiKey": "private-key"}]}},
        "rawPayload": '{"token":"private-raw"}',
    })
    assert response.status_code == 200, response.text
    assert response.json()["rawPayload"] is None
    assert response.json()["payload"]["data"]["nested"] == [{}]
    assert "private" not in response.text


def test_anomalies_keep_frontend_shape_and_filter_identity_mismatch():
    now = datetime.now(timezone.utc)
    cycle_id = f"audit-{uuid4()}"
    with Session(engine) as session:
        session.add(InboundMessage(
            msg_id=str(uuid4()), idpk=str(uuid4()), message_type="bid",
            cycle_id=cycle_id, sender="HGW", status="DISCARDED",
            reason_code="IDENTITY_MISMATCH", reason="Identidad de ciudad inválida",
            payload={"data": {"token": "private-value"}}, received_at=now,
        ))
        session.commit()
    result = client.get("/audit/anomalies", params={
        "cycleId": cycle_id, "sender": "HGW", "reasonCode": "IDENTITY_MISMATCH",
        "offset": 0, "limit": 1,
    })
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    assert result.json()["items"][0]["status"] == "DISCARDED"
    assert result.json()["items"][0]["reasonCode"] == "IDENTITY_MISMATCH"
    assert result.json()["items"][0]["sender"] == "HGW"
    assert "payload" not in result.json()["items"][0]
    assert "private-value" not in result.text
    assert client.get("/audit/anomalies", params={"cycleId": cycle_id, "offset": 1}).json()["items"] == []
