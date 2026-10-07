"""Regresiones de la auditoría integral E1 v2, además de negotiation-report."""
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlmodel import select

from tests.test_e1_v2_reports import context
from app.models import Cycle, LedgerEntry, OutboundMessage
from app.schemas import ProtocolMessageIn, OutboundMessageResultIn
from app.routers.internal_messages import ingest_protocol_message
from app.routers.message_audit import update_outbound_audit
from app.services.budget_carryover import synchronize_budget_carryover
from app.services.ledger import apply_ledger_effect, validate_cycle_consistency
from app.services.negotiation_proposals import enqueue_negotiation_proposal, enqueue_timed_out_negotiation_retries
from app.services.negotiation_state import transition_negotiation
from app.services.information_requests import enqueue_information_request


def effect(session, cycle, amount, **extra):
    return apply_ledger_effect(session, cycle_id=cycle.cycle_id, idpk=extra.get('idpk',str(uuid4())),
        source_msg_id=str(uuid4()), operation_type='TRANSFER_IN', budget_delta=Decimal(amount),
        energy_delta=Decimal('0'), details={})


def test_budget_carries_debt_and_late_adjustments_but_not_energy(context):
    session, first, now=context
    first.opening_energy_balance=first.energy_balance=Decimal('50')
    effect(session, first, '-50')
    second=Cycle(cycle_id='opaque-aaa-'+str(uuid4()), status_idpk=str(uuid4()),
        valid_until=first.valid_until+timedelta(hours=2), created_at=now,
        opening_energy_balance=Decimal('7'), energy_balance=Decimal('7'))
    session.add(second); session.flush()
    effect(session, second, '10')
    assert second.budget_balance == -40
    assert second.energy_balance == 7
    assert validate_cycle_consistency(session,second.cycle_id)==(Decimal('-40'),Decimal('7'))
    key=str(uuid4())
    effect(session,first,'30',idpk=key)
    effect(session,first,'30',idpk=key)
    assert first.budget_balance == -20
    assert second.budget_balance == -10
    entries=session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id==second.cycle_id,
        LedgerEntry.operation_type=='BUDGET_CARRYOVER')).all()
    assert [e.budget_delta for e in entries]==[Decimal('-50'),Decimal('30')]
    assert entries[-1].details['sourceCycleId']==first.cycle_id


def test_out_of_order_opening_reconciles_existing_cycle_without_rewriting_history(context):
    session, later, now=context
    effect(session,later,'20')
    earlier=Cycle(cycle_id='opaque-zzz-'+str(uuid4()),status_idpk=str(uuid4()),
        valid_until=later.valid_until-timedelta(hours=2),created_at=now)
    session.add(earlier);session.flush()
    effect(session,earlier,'100')
    assert later.budget_balance == 120
    assert validate_cycle_consistency(session,later.cycle_id)[0] == 120
    local=session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id==later.cycle_id,
        LedgerEntry.operation_type=='TRANSFER_IN')).one()
    assert local.budget_delta == 20 and local.budget_after == 20


def test_placeholder_does_not_supply_a_budget_carryover(context):
    session, cycle, now=context
    placeholder=Cycle(cycle_id='unknown-'+str(uuid4()),created_at=now)
    session.add(placeholder);session.flush()
    effect(session,placeholder,'500')
    synchronize_budget_carryover(session)
    assert cycle.budget_balance == 0


def proposal(context, direction='give'):
    session,cycle,now=context
    cycle.generation_capacity=Decimal('150');cycle.consumption=Decimal('100')
    cycle.generation_cost=Decimal('2.10')
    cycle.opening_energy_balance=cycle.energy_balance=Decimal('50')
    session.flush()
    negotiation=enqueue_negotiation_proposal(session,cycle_id=cycle.cycle_id,direction=direction,
        quantity=Decimal('3'),price_per_energy=Decimal('2.21'),city_id='KLD',routing_key='central',commit=False)
    return negotiation


def inbound(session,cycle,now,kind,data,**extra):
    payload=ProtocolMessageIn.model_validate(dict(type=kind,idpk=extra.get('idpk',str(uuid4())),
        msgId=extra.get('msgId',str(uuid4())),timestamp=now,sender='central',cycleId=cycle.cycle_id,data=data))
    ingest_protocol_message(payload,session)
    return payload


def test_ack_before_transport_callback_is_receipt_and_keeps_deadline(context):
    session,cycle,now=context
    negotiation=proposal(context)
    target=negotiation.latest_msg_id
    inbound(session,cycle,now,'ack',{'target':target})
    assert negotiation.status=='ACKNOWLEDGED'
    deadline=negotiation.deadline_at
    update_outbound_audit(target,OutboundMessageResultIn(status='PUBLISHED'),session)
    assert negotiation.status=='ACKNOWLEDGED' and negotiation.deadline_at==deadline
    assert not session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id==cycle.cycle_id)).all()


@pytest.mark.parametrize('direction,unit,total',[('give','2.21','6.63'),('take','2.10','6.30')])
def test_confirmation_before_callback_uses_rounded_settlement_price(context,direction,unit,total):
    session,cycle,now=context
    negotiation=proposal(context,direction)
    target=negotiation.latest_msg_id
    confirmation=inbound(session,cycle,now,direction,{'target':target,'energy':3,'pricePerEnergy':float(unit)})
    update_outbound_audit(target,OutboundMessageResultIn(status='PUBLISHED'),session)
    inbound(session,cycle,now,'ack',{'target':target})
    assert negotiation.status=='CONFIRMED'
    if direction=='give':
        inbound(session,cycle,now,'transfer',{'becauseOf':str(confirmation.msgId),'quantity':float(total)})
        assert cycle.budget_balance==Decimal(total)
    else:
        payment=session.exec(select(OutboundMessage).where(OutboundMessage.cycle_id==cycle.cycle_id,
            OutboundMessage.message_type=='transfer')).one()
        assert Decimal(str(payment.payload['data']['quantity']))==Decimal(total)
        assert cycle.budget_balance==-Decimal(total)
        update_outbound_audit(payment.msg_id,OutboundMessageResultIn(status='PUBLISHED'),session)
    assert negotiation.status=='PAID'
    assert validate_cycle_consistency(session,cycle.cycle_id)==(cycle.budget_balance,cycle.energy_balance)


def test_delayed_give_payment_after_retry_is_correlated_and_applied_once(context):
    session,cycle,now=context
    negotiation=proposal(context)
    original=negotiation.latest_msg_id
    confirmation=inbound(session,cycle,now,'give',{'target':original,'energy':3,'pricePerEnergy':2.21})
    transition_negotiation(session,negotiation,'TIMEOUT',now=now+timedelta(seconds=31))
    assert enqueue_timed_out_negotiation_retries(session,city_id='KLD',routing_key='central',now=now+timedelta(seconds=32))==1
    retry=negotiation.latest_msg_id
    assert retry != original
    inbound(session,cycle,now,'transfer',{'becauseOf':str(confirmation.msgId),'quantity':6.63})
    inbound(session,cycle,now,'transfer',{'becauseOf':str(confirmation.msgId),'quantity':6.63})
    assert negotiation.status=='PAID' and cycle.budget_balance==Decimal('6.63')
    assert cycle.energy_balance==47
    assert not session.exec(select(OutboundMessage).where(OutboundMessage.msg_id==retry)).one().dispatch_required


def test_confirmation_for_older_proposal_after_retry_is_not_lost(context):
    session,cycle,now=context
    negotiation=proposal(context)
    original=negotiation.latest_msg_id
    update_outbound_audit(original,OutboundMessageResultIn(status='PUBLISHED'),session)
    transition_negotiation(session,negotiation,'TIMEOUT',now=now+timedelta(seconds=31))
    enqueue_timed_out_negotiation_retries(session,city_id='KLD',routing_key='central',now=now+timedelta(seconds=32))
    inbound(session,cycle,now,'give',{'target':original,'energy':3,'pricePerEnergy':2.21})
    assert negotiation.status=='CONFIRMED' and cycle.energy_balance==47


def test_confirmation_wrong_price_is_audited_without_ledger_effect(context):
    session,cycle,now=context
    negotiation=proposal(context)
    with pytest.raises(HTTPException) as exc:
        inbound(session,cycle,now,'give',{'target':negotiation.latest_msg_id,'energy':3,'pricePerEnergy':2.20})
    assert exc.value.status_code==422
    assert not session.exec(select(LedgerEntry).where(LedgerEntry.cycle_id==cycle.cycle_id)).all()


def test_request_ask_is_open_string_without_cycle(context):
    session,cycle,now=context
    result,_=enqueue_information_request(session,ask='future-information',city_id='KLD',routing_key='central',commit=False)
    assert result.payload['data']['ask']=='future-information'
    assert 'cycleId' not in result.payload



def test_pending_proposal_expires_without_worker_or_publication(context):
    from app.routers.message_audit import list_outbound_dispatch
    session, cycle, now = context
    negotiation = proposal(context)
    target = negotiation.latest_msg_id
    cycle.valid_until = now - timedelta(seconds=1)
    session.flush()
    result = list_outbound_dispatch(limit=100, session=session)
    assert not any(item['msgId'] == target for item in result['items'])
    assert negotiation.status == 'TIMEOUT'
    outbound = session.exec(select(OutboundMessage).where(OutboundMessage.msg_id == target)).one()
    assert not outbound.dispatch_required and outbound.last_error == 'CYCLE_EXPIRED'


def test_nack_before_transport_callback_remains_rejected(context):
    session, cycle, now = context
    negotiation = proposal(context)
    target = negotiation.latest_msg_id
    payload = ProtocolMessageIn.model_validate(dict(type='nack', idpk=str(uuid4()), msgId=str(uuid4()),
        timestamp=now, sender='central', reason='MALFORMED_MESSAGE', code=422,
        data={'target': target, 'message': 'rejected'}))
    ingest_protocol_message(payload, session)
    update_outbound_audit(target, OutboundMessageResultIn(status='PUBLISHED'), session)
    assert negotiation.status == 'REJECTED'
    assert not session.exec(select(OutboundMessage).where(OutboundMessage.msg_id == target)).one().dispatch_required



def test_report_for_unknown_cycle_is_rejected_without_creating_cycle(context):
    session, cycle, now = context
    unknown = 'unknown-report-' + str(uuid4())
    payload = ProtocolMessageIn.model_validate(dict(type='negotiation-report', idpk=str(uuid4()),
        msgId=str(uuid4()), timestamp=now, cityId='KLD', cycleId=unknown,
        data={'energyBalance': 0, 'budgetBalance': 0}))
    with pytest.raises(HTTPException) as exc:
        ingest_protocol_message(payload, session)
    assert exc.value.status_code == 422
    assert exc.value.detail['reason'] == 'CYCLE_UNKNOWN'
    assert session.get(Cycle, unknown) is None



def test_zero_cost_give_accepts_exact_zero_payment(context):
    session, cycle, now = context
    cycle.generation_capacity = Decimal('150')
    cycle.consumption = Decimal('100')
    cycle.generation_cost = Decimal('0')
    cycle.opening_energy_balance = cycle.energy_balance = Decimal('50')
    negotiation = enqueue_negotiation_proposal(session, cycle_id=cycle.cycle_id, direction='give',
        quantity=Decimal('3'), price_per_energy=Decimal('0'), city_id='KLD', routing_key='central', commit=False)
    confirmation = inbound(session, cycle, now, 'give',
        {'target': negotiation.latest_msg_id, 'energy': 3, 'pricePerEnergy': 0})
    inbound(session, cycle, now, 'transfer', {'becauseOf': str(confirmation.msgId), 'quantity': 0})
    assert negotiation.status == 'PAID' and negotiation.payment_quantity == 0
    assert cycle.energy_balance == 47 and cycle.budget_balance == 0



def test_observed_distance_table_reaches_connectivity(context):
    from app.models import InboundMessage
    from app.routers.connectivity import get_connectivity
    session, _, _ = context
    # Metadata productiva informada; identificadores y distancias sintéticos.
    payload = ProtocolMessageIn.model_validate(dict(
        idpk=str(uuid4()), msgId=str(uuid4()), type='distance-table',
        sender='central', cityId=None, cycleId='cycle-248784',
        timestamp='2026-10-05T23:40:04.832000Z',
        data={'distances': {'HGW': {'distance': 10, 'transportCost': 0.1, 'enabled': True}}}))
    ingest_protocol_message(payload, session)
    audit = session.exec(select(InboundMessage).where(InboundMessage.msg_id == str(payload.msgId))).one()
    assert audit.status == 'PROCESSED'
    assert audit.payload['cityId'] is None
    assert audit.payload['cycleId'] == 'cycle-248784'
    connectivity = get_connectivity(session)
    assert connectivity.timestamp == payload.timestamp
    assert connectivity.total == 1
    assert connectivity.items[0].destination == 'HGW'
    assert connectivity.items[0].distance == 10
    assert connectivity.items[0].transportCost == 0.1
    assert connectivity.items[0].enabled is True
