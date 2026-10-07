import json
from uuid import uuid4
import pytest
from protocol.handler import plan_protocol_response
from protocol.intake import decode_incoming_payload, DiscardMessage
from protocol.publisher import build_amqp_message
from protocol.validation import validate_payload
import aio_pika


def message(kind="request", **extra):
    return dict(idpk=str(uuid4()), msgId=str(uuid4()), type=kind, timestamp="2026-10-07T10:00:00Z",
                cityId="KLD", data={"ask": "future-information-type"}, **extra)


@pytest.mark.parametrize("msg_id", [None, "", "invalid", [], 42])
def test_no_valid_correlation_id_is_discarded(msg_id):
    raw=message();raw["msgId"]=msg_id
    with pytest.raises(DiscardMessage):
        decode_incoming_payload(json.dumps(raw).encode())
    assert plan_protocol_response(raw, "KLD").action == "discard"


@pytest.mark.parametrize("kind", ["ack", "nack", "error"])
def test_malformed_responses_never_trigger_response_loops(kind):
    raw=message(kind)
    raw.pop("idpk")
    assert plan_protocol_response(raw, "KLD").action == "discard"
    raw=message(kind)
    raw["data"]={}
    assert plan_protocol_response(raw, "KLD").response is None


@pytest.mark.parametrize("user_id", [None, "city.COR", "KLD"])
def test_city_identity_requires_matching_amqp_property(user_id):
    result=plan_protocol_response(message(), "KLD", amqp_user_id=user_id)
    assert (result.response["reason"], result.response["code"]) == ("IDENTITY_MISMATCH",403)


def test_request_unknown_ask_is_valid_but_cycle_is_forbidden():
    raw=message()
    assert plan_protocol_response(raw,"KLD",amqp_user_id="city.KLD").action == "ack"
    raw["cycleId"]="opaque"
    assert not validate_payload(raw)[1].valid


def test_distance_table_must_identify_central():
    raw=message("distance-table");raw["data"]={"distances":{}}
    assert not validate_payload(raw)[1].valid
    raw.pop("cityId");raw["sender"]="central"
    assert validate_payload(raw)[1].valid


def test_outgoing_message_sets_amqp_identity_and_persistence():
    raw=message();amqp=build_amqp_message(raw,"city.KLD")
    assert amqp.user_id == "city.KLD"
    assert amqp.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert "user_id" not in json.loads(amqp.body)
    with pytest.raises(ValueError):
        build_amqp_message(raw,"city.COR")


@pytest.mark.parametrize("value", [float('inf'), float('-inf'), float('nan')])
def test_nonfinite_money_is_not_valid_protocol(value):
    raw=message("transfer");raw['cycleId']='opaque';raw['data']={'quantity':value}
    assert not validate_payload(raw)[1].valid


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_json_is_discarded_before_http_audit(number):
    with pytest.raises(DiscardMessage):
        decode_incoming_payload(('{"data":' + number + '}').encode())


def test_invalid_metadata_does_not_crash_or_echo_invalid_cycle():
    raw = message(); raw['type'] = []; raw['cycleId'] = {}
    result = plan_protocol_response(raw, 'KLD')
    assert result.action == 'nack'
    assert 'cycleId' not in result.response['data']
    raw = message('nack'); raw['reason'] = []
    assert plan_protocol_response(raw, 'KLD').action == 'discard'


@pytest.mark.parametrize('include_city', [False, True])
@pytest.mark.parametrize('include_cycle', [False, True])
def test_distance_table_observed_central_envelope(include_city, include_cycle):
    # Reproducir la metadata productiva informada; los UUID y las distancias son sintéticos.
    raw = message('distance-table')
    raw.pop('cityId')
    raw.update(sender='central', timestamp='2026-10-05T23:40:04.832000Z',
               data={'distances': {'HGW': {'distance': 10, 'transportCost': 0.1, 'enabled': True}}})
    if include_city:
        raw['cityId'] = None
    if include_cycle:
        raw['cycleId'] = 'cycle-248784'
    decoded = decode_incoming_payload(json.dumps(raw).encode())
    result = plan_protocol_response(decoded, 'KLD')
    assert result.action == 'ack'
    assert result.message.sender == 'central'
    assert result.message.city_id is None
    assert result.message.cycle_id == ('cycle-248784' if include_cycle else None)
    assert result.response['data']['target'] == raw['msgId']
