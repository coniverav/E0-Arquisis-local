"""Reportes AD8 en PostgreSQL aislado, sin conexión al broker."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine
from app.main import app
from app.models import Cycle, NegotiationReport, OutboundMessage
from app.services.negotiation_report_dispatch import enqueue_due_negotiation_reports, process_report_error
from app.services.outbox_claims import claim_batch
from tests.test_e3028_market_outbox_db import report_scenario
from tests.test_e1_v2_reports import error


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv('E3_CYCLE_REPORTS_OUTBOX_ENABLED', 'true')
    return TestClient(app)


def enqueue():
    with Session(engine) as session:
        count = enqueue_due_negotiation_reports(session, city_id='KLD', routing_key='central')
        session.commit()
        return count


def claims(client):
    response = client.post('/internal/outbox-relay/claims', json={
        'workerId': 'test-relay', 'deliveryKind': 'BROKER'})
    assert response.status_code == 200
    return response.json()['items']


def test_crash_after_commit_and_lost_publish_ack_replays_exact_envelope(report_scenario, relay):
    assert enqueue() == 1  # caída después del commit, antes del broker
    assert enqueue() == 0
    first = claims(relay)[0]
    with Session(engine) as session:
        row = session.get(OutboundMessage, first['id'])
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()  # simula muerte tras publicar sin guardar el resultado
    second = claims(relay)[0]
    assert first['payload'] == second['payload']
    assert first['idpk'] == second['idpk'] and first['msgId'] == second['msgId']
    assert first['claimToken'] != second['claimToken']
    assert relay.post(f"/internal/outbox-relay/{first['id']}/result", json={
        'claimToken': first['claimToken'], 'outcome': 'PUBLISHED'}).status_code == 409
    assert relay.post(f"/internal/outbox-relay/{second['id']}/result", json={
        'claimToken': second['claimToken'], 'outcome': 'PUBLISHED'}).status_code == 200
    assert claims(relay) == []
    with Session(engine) as session:
        assert session.exec(select(NegotiationReport)).one().status == 'PUBLISHED'


def test_failure_before_commit_rolls_back_business_and_intent(report_scenario, monkeypatch):
    from app.services import negotiation_report_dispatch as dispatch
    original = dispatch.enqueue_market_envelope
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(dispatch, 'enqueue_market_envelope', fail)
    with pytest.raises(RuntimeError):
        enqueue()
    with Session(engine) as session:
        assert session.exec(select(NegotiationReport)).all() == []
        assert session.exec(select(OutboundMessage)).all() == []


def test_cutover_excludes_legacy_and_rollback_recovers_expired_claim(report_scenario, relay, monkeypatch):
    enqueue()
    assert relay.get('/internal/audit/outbound/dispatch').json()['items'] == []
    item = claims(relay)[0]
    assert relay.post(f"/internal/audit/outbound/{item['msgId']}/reserve-quota").status_code == 409
    assert relay.post(f"/internal/audit/outbound/{item['msgId']}/result",
                      json={'status': 'PUBLISHED'}).status_code == 409
    monkeypatch.setenv('E3_CYCLE_REPORTS_OUTBOX_ENABLED', 'false')
    assert claims(relay) == []
    assert relay.get('/internal/audit/outbound/dispatch').json()['items'] == []
    with Session(engine) as session:
        row = session.get(OutboundMessage, item['id'])
        row.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        session.commit()
    legacy = relay.get('/internal/audit/outbound/dispatch').json()['items']
    assert len(legacy) == 1 and legacy[0]['payload'] == item['payload']


@pytest.mark.parametrize('boundary', ['before', 'expired', 'closed'])
def test_relay_rechecks_central_window_without_scheduler(report_scenario, relay, boundary):
    enqueue()
    with Session(engine) as session:
        cycle = session.get(Cycle, report_scenario.cid)
        if boundary == 'before':
            cycle.report_window_opens_at = datetime.now(timezone.utc) + timedelta(seconds=20)
        elif boundary == 'expired':
            cycle.valid_until = datetime.now(timezone.utc)
        else:
            cycle.scheduler_state = 'CLOSED'
        session.add(cycle)
        session.commit()
    assert claims(relay) == []
    with Session(engine) as session:
        if boundary != 'before':
            assert session.exec(select(NegotiationReport)).one().status == 'EXPIRED'
            assert session.exec(select(OutboundMessage)).one().dispatch_required is False


def test_report_error_fences_late_publisher_confirmation(report_scenario, relay):
    enqueue()
    item = claims(relay)[0]
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        cycle = session.get(Cycle, report_scenario.cid)
        row = session.get(OutboundMessage, item['id'])
        process_report_error(session, error(cycle, row, now), now=now)
        session.commit()
    assert relay.post(f"/internal/outbox-relay/{item['id']}/result", json={
        'claimToken': item['claimToken'], 'outcome': 'PUBLISHED'}).status_code == 409
    assert claims(relay) == []
    with Session(engine) as session:
        rows = session.exec(select(OutboundMessage).order_by(OutboundMessage.id)).all()
        assert len(rows) == 2 and rows[1].idpk == rows[0].idpk
        assert rows[1].msg_id != rows[0].msg_id  # intento autorizado por Central
        assert rows[1].payload['data'] == rows[0].payload['data']
        assert session.exec(select(NegotiationReport)).one().status == 'DEFERRED'


def test_report_versions_are_claimed_in_order(report_scenario):
    enqueue()
    with Session(engine) as session:
        original = session.exec(select(OutboundMessage)).one()
        # Dos intenciones históricas pendientes también conservan el orden.
        data = original.model_dump(exclude={'id'})
        data.update(msg_id=str(uuid4()), idpk=str(uuid4()))
        data['payload'] = dict(original.payload, msgId=data['msg_id'], idpk=data['idpk'])
        session.add(OutboundMessage(**data))
        session.commit()
        first = claim_batch(session, worker_id='first', kinds=('BROKER',))
        session.commit()
        assert len(first) == 1 and first[0].id == original.id
        assert claim_batch(session, worker_id='second', kinds=('BROKER',)) == []


def test_report_does_not_consume_market_or_wallet_quota(report_scenario, relay, monkeypatch):
    enqueue()
    def unexpected(*args, **kwargs):
        raise AssertionError('El contrato no asigna esta cuota a negotiation-report')
    monkeypatch.setattr('app.routers.outbox_relay.reserve_rate_limit', unexpected)
    assert len(claims(relay)) == 1
