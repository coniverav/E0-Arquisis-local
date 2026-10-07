from uuid import uuid4

import pytest

from protocol.handler import plan_protocol_response
from protocol.validation import validate_payload


def early_error(code=425):
    return dict(idpk=str(uuid4()), msgId=str(uuid4()), type="error", sender="central",
        timestamp="2026-10-07T10:00:00Z", cycleId="v2-test", reason="REPORT_TOO_EARLY", code=code,
        data={"target": str(uuid4()), "message": "Wait for closing period", "opensAt": "2026-10-07T10:05:00Z"})


@pytest.mark.parametrize("code", [422, 425])
def test_early_report_error_is_business_error_not_nack(code):
    payload = early_error(code)
    assert validate_payload(payload)[1].valid
    assert plan_protocol_response(payload, city_id="KLD").action == "ignore"


@pytest.mark.parametrize("opens_at", [None, "2026-10-07T10:05:00", "invalid", 123])
def test_opens_at_requires_iso_timestamp_with_timezone(opens_at):
    payload = early_error()
    if opens_at is None:
        del payload["data"]["opensAt"]
    else:
        payload["data"]["opensAt"] = opens_at
    result = validate_payload(payload)[1]
    assert not result.valid and result.reason == "MALFORMED_MESSAGE"


def test_report_without_cycle_is_malformed_422():
    payload = dict(idpk=str(uuid4()), msgId=str(uuid4()), type="negotiation-report", cityId="KLD",
        timestamp="2026-10-07T10:00:00Z", data={"budgetBalance": 0, "energyBalance": 0})
    result = validate_payload(payload)[1]
    assert (result.reason, result.code) == ("MALFORMED_MESSAGE", 422)


def test_early_report_does_not_accept_arbitrary_code_or_nack_type():
    payload = early_error(409)
    assert not validate_payload(payload)[1].valid
    payload = early_error()
    payload["type"] = "nack"
    assert not validate_payload(payload)[1].valid
