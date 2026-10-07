"""Regresiones E1 v2 sobre PostgreSQL migrado, sin credenciales del broker."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import ingest_protocol_error
from app.models import Cycle, NegotiationReport, OutboundMessage, LedgerEntry
from app.routers.cycles import cycle_detail
from app.routers.internal_messages import ingest_protocol_message
from app.routers.message_audit import list_outbound_dispatch, update_outbound_audit
from app.schemas import ProtocolErrorPayload, ProtocolMessageIn, OutboundMessageResultIn
from app.services.cycle_scheduler import configure_cycle_schedule
from app.services.ledger import apply_ledger_effect
from app.services.negotiation_report import generate_negotiation_report
from app.services.negotiation_report_dispatch import enqueue_due_negotiation_reports, process_report_error


@pytest.fixture
def context():
    run_migrations()
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(connection, join_transaction_mode="create_savepoint") as session:
            now = datetime.now(timezone.utc)
            cycle = Cycle(cycle_id=f"v2-{uuid4()}", status_idpk=str(uuid4()),
                valid_until=now+timedelta(minutes=2), report_window_opens_at=now-timedelta(minutes=3),
                scheduler_state="REPORT_WINDOW", created_at=now)
            session.add(cycle)
            session.flush()
            yield session, cycle, now
        transaction.rollback()


def enqueue(session, now):
    return enqueue_due_negotiation_reports(session, city_id="KLD", routing_key="central", now=now)


def attempts(session, cycle):
    return session.exec(select(OutboundMessage).where(
        OutboundMessage.cycle_id == cycle.cycle_id,
        OutboundMessage.message_type == "negotiation-report"
    ).order_by(OutboundMessage.id)).all()


def error(cycle, target, now, *, reason="REPORT_TOO_EARLY", code=425, opens_at=None):
    data = {"target": target.msg_id, "message": reason}
    if reason == "REPORT_TOO_EARLY":
        data["opensAt"] = (opens_at or now+timedelta(seconds=45)).isoformat()
    return ProtocolErrorPayload.model_validate(dict(
        idpk=str(uuid4()), msgId=str(uuid4()), type="error", sender="central",
        timestamp=now, cycleId=cycle.cycle_id, reason=reason, code=code, data=data,
    ))


def dispatch(session, now):
    with patch("app.routers.message_audit.datetime") as clock:
        clock.now.return_value = now
        return [item for item in list_outbound_dispatch(limit=100, session=session)["items"]
                if item["type"] == "negotiation-report"]


def test_window_boundaries_and_no_phantom_cycle(context):
    session, cycle, now = context
    cycle.report_window_opens_at = now+timedelta(seconds=10)
    assert enqueue(session, now) == 0  # incluso con el estado REPORT_WINDOW desactualizado
    with pytest.raises(ValueError, match="REPORT_TOO_EARLY"):
        generate_negotiation_report(session, cycle_id=cycle.cycle_id, city_id="KLD", now=now)
    assert enqueue(session, cycle.report_window_opens_at) == 1
    assert enqueue(session, cycle.valid_until) == 0
    assert not dispatch(session, cycle.valid_until)
    assert cycle_detail(cycle.cycle_id, session).negotiationReport.status == "EXPIRED"


def test_cycle_without_central_opening_never_reports(context):
    session, cycle, now = context
    cycle.status_idpk = None
    assert enqueue(session, now) == 0
    with pytest.raises(ValueError, match="CYCLE_UNKNOWN"):
        generate_negotiation_report(session, cycle_id=cycle.cycle_id, city_id="KLD", now=now)


@pytest.mark.parametrize("code", [422, 425])
def test_too_early_persists_retry_and_survives_new_session(context, code):
    session, cycle, now = context
    assert enqueue(session, now) == 1
    original = attempts(session, cycle)[0]
    payload = error(cycle, original, now, code=code)
    ingest_protocol_error(payload, session)
    ingest_protocol_error(payload, session)  # reentrega del mismo error
    queued = attempts(session, cycle)
    assert len(queued) == 2
    retry = queued[-1]
    assert retry.idpk == original.idpk and retry.msg_id != original.msg_id
    assert retry.payload["data"] == original.payload["data"]
    assert retry.available_at == payload.data.opensAt
    assert not original.dispatch_required
    # Una publicación tardía no debe borrar DEFERRED ni su motivo de negocio.
    update_outbound_audit(original.msg_id, OutboundMessageResultIn(status="PUBLISHED"), session)
    assert cycle_detail(cycle.cycle_id, session).negotiationReport.status == "DEFERRED"
    configure_cycle_schedule(cycle, valid_until=cycle.valid_until)
    assert cycle.report_window_opens_at == payload.data.opensAt
    retry_msg_id = retry.msg_id
    cycle_id = cycle.cycle_id
    session.commit()
    session.close()
    # Abrir otra sesión como un worker nuevo: la programación debe persistir.
    with Session(session.bind, join_transaction_mode="create_savepoint") as restarted:
        assert not dispatch(restarted, now)
        assert not dispatch(restarted, payload.data.opensAt-timedelta(microseconds=1))
        due = dispatch(restarted, payload.data.opensAt)
        assert [item["msgId"] for item in due] == [retry_msg_id]
        update_outbound_audit(retry_msg_id, OutboundMessageResultIn(status="PUBLISHED"), restarted)
        assert cycle_detail(cycle_id, restarted).negotiationReport.status == "PUBLISHED"
        assert not dispatch(restarted, payload.data.opensAt)


@pytest.mark.parametrize("offset", [0, -10, 120, 150])
def test_invalid_or_unusable_opens_at_never_hot_loops(context, offset):
    session, cycle, now = context
    enqueue(session, now)
    process_report_error(session, error(cycle, attempts(session, cycle)[0], now,
        opens_at=now+timedelta(seconds=offset)), now=now)
    assert len(attempts(session, cycle)) == 1
    assert enqueue(session, now) == 0
    assert not dispatch(session, now)


def test_expired_error_is_terminal(context):
    session, cycle, now = context
    enqueue(session, now)
    target = attempts(session, cycle)[0]
    process_report_error(session, error(cycle, target, now, reason="CYCLE_EXPIRED", code=410), now=now)
    process_report_error(session, error(cycle, target, now), now=now)
    assert cycle.scheduler_state == "CLOSED"
    assert enqueue(session, now) == 0
    assert not dispatch(session, now)
    assert cycle_detail(cycle.cycle_id, session).negotiationReport.status == "EXPIRED"


def test_corrections_and_same_idpk_retry_keep_history_and_ledger(context):
    session, cycle, now = context
    enqueue(session, now)
    first = cycle_detail(cycle.cycle_id, session).negotiationReport
    for index in range(2):
        apply_ledger_effect(session, cycle_id=cycle.cycle_id, idpk=str(uuid4()), source_msg_id=str(uuid4()),
            operation_type="DEMAND_STATEMENT", budget_delta=Decimal("-10"), energy_delta=Decimal("2"), details={})
        assert enqueue(session, now+timedelta(seconds=index+1)) == 1
    detail = cycle_detail(cycle.cycle_id, session)
    assert len(detail.negotiationReports) == 3
    assert len({report.idpk for report in detail.negotiationReports}) == 3
    assert detail.negotiationReport.budgetBalance == Decimal("-20")
    retry = generate_negotiation_report(session, cycle_id=cycle.cycle_id, city_id="KLD", idpk=first.idpk, now=now)
    assert retry.msg_id == first.msgId and retry.budget_balance == 0
    assert len(detail.ledger) == 2
    assert len(dispatch(session, now+timedelta(seconds=5))) == 1
    # Una corrección explícita es válida aunque conserve los saldos reportados.
    corrected = generate_negotiation_report(session, cycle_id=cycle.cycle_id, city_id="KLD", idpk=str(uuid4()), now=now)
    assert corrected.idpk != detail.negotiationReport.idpk


def test_inbound_report_corrections_and_missing_cycle(context):
    session, cycle, now = context
    payload = dict(idpk=str(uuid4()), msgId=str(uuid4()), type="negotiation-report", cityId="KLD",
        timestamp=now.isoformat(), cycleId=cycle.cycle_id, data={"budgetBalance": 0, "energyBalance": 0})
    missing = {k:v for k,v in payload.items() if k != "cycleId"}
    with pytest.raises(ValidationError, match="MALFORMED_MESSAGE"):
        ProtocolMessageIn.model_validate(missing)
    for index in range(3):
        payload.update(idpk=str(uuid4()), msgId=str(uuid4()), data={"budgetBalance": index, "energyBalance": 0})
        assert ingest_protocol_message(ProtocolMessageIn.model_validate(payload), session)["status"] == "ok"
        payload["msgId"] = str(uuid4())
        assert ingest_protocol_message(ProtocolMessageIn.model_validate(payload), session)["status"] == "duplicate"
    detail = cycle_detail(cycle.cycle_id, session)
    assert len(detail.negotiationReports) == 3
    assert detail.negotiationReport.budgetBalance == 2
    assert not detail.ledger


@pytest.mark.parametrize("penalty", [50000, {"undocumented": 50000}, ["opaque"], None])
def test_penalty_is_preserved_without_double_deduction(context, penalty):
    session, cycle, now = context
    payload = ProtocolMessageIn.model_validate(dict(idpk=str(uuid4()), msgId=str(uuid4()), type="transfer",
        sender="central", timestamp=now, cycleId=cycle.cycle_id, data={"quantity": 70000, "penalty": penalty}))
    ingest_protocol_message(payload, session)
    ingest_protocol_message(payload, session)
    session.refresh(cycle)
    assert cycle.budget_balance == 70000
    entry = session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle.cycle_id)).one()
    assert entry.details["data"]["penalty"] == penalty


def test_transfer_preserves_unmodeled_envelope_penalty(context):
    session, cycle, now = context
    payload = ProtocolMessageIn.model_validate(dict(idpk=str(uuid4()), msgId=str(uuid4()), type="transfer",
        sender="central", timestamp=now, cycleId=cycle.cycle_id, data={"quantity": 70000}, penalty={"opaque": 50000}))
    ingest_protocol_message(payload, session)
    entry = session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id == cycle.cycle_id)).one()
    assert entry.details["penalty"] == {"opaque": 50000}
    assert entry.budget_delta == 70000


@pytest.mark.parametrize("opens_at", [None, "2026-10-07T12:00:00", "invalid"])
def test_master_requires_valid_opens_at(context, opens_at):
    session, cycle, now = context
    enqueue(session, now)
    raw = error(cycle, attempts(session, cycle)[0], now).model_dump(mode="json")
    raw["data"]["opensAt"] = opens_at
    with pytest.raises(ValidationError):
        ProtocolErrorPayload.model_validate(raw)


def test_transport_failure_then_publication_marks_report_published(context):
    session, cycle, now = context
    enqueue(session, now)
    target = attempts(session, cycle)[0]
    update_outbound_audit(target.msg_id, OutboundMessageResultIn(status="FAILED", error="broker unavailable"), session)
    assert len(dispatch(session, now)) == 1
    update_outbound_audit(target.msg_id, OutboundMessageResultIn(status="PUBLISHED"), session)
    assert cycle_detail(cycle.cycle_id, session).negotiationReport.status == "PUBLISHED"


def test_expired_inbound_report_is_not_recorded(context):
    from fastapi import HTTPException
    session, cycle, now = context
    cycle.valid_until = now-timedelta(seconds=1)
    session.commit()
    payload = ProtocolMessageIn.model_validate(dict(idpk=str(uuid4()), msgId=str(uuid4()), type="negotiation-report",
        cityId="KLD", timestamp=now, cycleId=cycle.cycle_id, data={"budgetBalance": 0, "energyBalance": 0}))
    with pytest.raises(HTTPException) as exc:
        ingest_protocol_message(payload, session)
    assert exc.value.status_code == 410
    assert not session.exec(select(NegotiationReport).where(NegotiationReport.cycle_id == cycle.cycle_id)).all()
