"""E2-071: cupos y cooldown compartidos, sin servicios externos."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import Cycle, OperationalRateLimit, OutboundMessage
from app.services.operational_rate_limits import block, reserve
from app.services.wallet_client import WalletClient, WalletError, WalletRateLimitError


def test_sliding_window_is_shared_between_sessions_and_survives_reopen():
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(Cycle(cycle_id=f"limit-{uuid4()}", status_idpk=str(uuid4()),
                          market_opens_at=now,
                          status_payload={"market": {"limits": {"walletReadsPerMinute": 2}}},
                          created_at=now))
        session.commit()
    with Session(engine) as session:
        assert reserve(session, "wallet:read", now=now) == 0
        session.commit()
    with Session(engine) as session:
        assert reserve(session, "wallet:read", now=now + timedelta(seconds=1)) == 0
        session.commit()
    with Session(engine) as session:
        assert reserve(session, "wallet:read", now=now + timedelta(seconds=5)) == 55
        session.commit()
    with Session(engine) as session:
        assert reserve(session, "wallet:read", now=now + timedelta(seconds=61)) == 0
        session.commit()


def test_retry_after_120_blocks_other_instances_at_5_and_61_seconds():
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        block(session, "wallet:read", 120, now=now)
        session.commit()
    for elapsed, wait in ((5, 115), (61, 59), (121, 0)):
        with Session(engine) as session:
            assert reserve(session, "wallet:read", now=now + timedelta(seconds=elapsed)) == wait
            session.commit()


def test_wallet_429_and_503_parse_official_integer_retry_after():
    client = WalletClient(base_url="https://wallet.test/v1", api_key="test-only")
    with pytest.raises(WalletRateLimitError) as limited:
        client._handle_response_error(httpx.Response(429, headers={"Retry-After": "120"}), "wallet:read")
    assert limited.value.retry_after == 120
    with pytest.raises(WalletError) as busy:
        client._handle_response_error(httpx.Response(503, headers={"Retry-After": "7"}), "wallet:write")
    assert busy.value.retry_after == 7
    with Session(engine) as session:
        buckets = session.exec(select(OperationalRateLimit)).all()
    assert any(bucket.key.endswith(":wallet:read") and bucket.blocked_until for bucket in buckets)
    assert any(bucket.key.endswith(":wallet:write") and bucket.blocked_until for bucket in buckets)


def test_market_quota_defers_outbound_without_losing_message():
    now = datetime.now(timezone.utc)
    first, second = str(uuid4()), str(uuid4())
    with Session(engine) as session:
        session.add(Cycle(cycle_id=f"market-limit-{uuid4()}", status_idpk=str(uuid4()),
                          market_opens_at=now,
                          status_payload={"market": {"limits": {"marketOpsPerMinute": 1}}},
                          created_at=now))
        for msg_id in (first, second):
            session.add(OutboundMessage(msg_id=msg_id, idpk=str(uuid4()), message_type="offer",
                                        payload={"msgId": msg_id}, status="PENDING",
                                        dispatch_required=True, created_at=now,
                                        expires_at=now + timedelta(minutes=2)))
        session.commit()
    client = TestClient(app)
    granted = client.post(f"/internal/audit/outbound/{first}/reserve-quota")
    deferred = client.post(f"/internal/audit/outbound/{second}/reserve-quota")
    assert granted.status_code == 200 and granted.json()["allowed"] is True
    assert deferred.status_code == 200 and deferred.json()["allowed"] is False
    with Session(engine) as session:
        message = session.exec(select(OutboundMessage).where(OutboundMessage.msg_id == second)).one()
        assert message.status == "PENDING" and message.dispatch_required
        assert message.available_at > now


def test_request_ask_has_its_own_ten_per_minute_budget():
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    with Session(engine) as session:
        for second in range(10):
            assert reserve(session, "central:request", now=now + timedelta(seconds=second)) == 0
        session.commit()
    with Session(engine) as session:
        assert reserve(session, "central:request", now=now + timedelta(seconds=10)) == 50
        session.commit()


def test_concurrent_processes_cannot_both_consume_last_market_slot():
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        session.add(Cycle(cycle_id=f"concurrent-limit-{uuid4()}", status_idpk=str(uuid4()),
                          market_opens_at=now,
                          status_payload={"market": {"limits": {"marketOpsPerMinute": 1}}},
                          created_at=now))
        session.commit()

    def try_reserve():
        with Session(engine) as session:
            result = reserve(session, "market:offer", now=now)
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: try_reserve(), range(2)))
    assert sorted(results) == [0, 60]
