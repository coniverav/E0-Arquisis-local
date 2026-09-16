import unittest
from uuid import UUID
from pydantic import ValidationError
from app.schemas import ProtocolErrorPayload

#Construye un payload error válido y permite sobrescribir campos.
def valid_error_payload(**overrides):
    payload = {
        "idpk": "550e8400-e29b-41d4-a716-446655440000",
        "msgId": "550e8400-e29b-41d4-a716-446655440001",
        "type": "error",
        "timestamp": "2026-09-16T02:00:00Z",
        "sender": "central",
        "cycleId": "cycle-9431",
        "reason": "CYCLE_UNKNOWN",
        "code": 404,
        "data": {
            "target": "550e8400-e29b-41d4-a716-446655440002",
            "message": "El ciclo no existe",
        },
    }

    payload.update(overrides)
    return payload

class ProtocolErrorPayloadTests(unittest.TestCase):
    #Acepta CYCLE_UNKNOWN cuando utiliza el código 404.
    def test_valid_cycle_unknown(self):
        payload = ProtocolErrorPayload.model_validate(
            valid_error_payload()
        )

        self.assertEqual(payload.reason, "CYCLE_UNKNOWN")
        self.assertEqual(payload.code, 404)

    #Acepta CYCLE_EXPIRED cuando utiliza el código 410.
    def test_valid_cycle_expired(self):
        payload = ProtocolErrorPayload.model_validate(
            valid_error_payload(
                reason="CYCLE_EXPIRED",
                code=410,
            )
        )

        self.assertEqual(payload.reason, "CYCLE_EXPIRED")
        self.assertEqual(payload.code, 410)

    #Rechaza PRICE_ABOVE_CAP cuando falta data.cap.
    def test_price_above_cap_requires_cap(self):
        with self.assertRaises(ValidationError):
            ProtocolErrorPayload.model_validate(
                valid_error_payload(
                    reason="PRICE_ABOVE_CAP",
                    code=422,
                )
            )

    #Acepta PRICE_ABOVE_CAP cuando incluye data.cap.
    def test_valid_price_above_cap(self):
        raw = valid_error_payload(
            reason="PRICE_ABOVE_CAP",
            code=422,
        )
        raw["data"]["cap"] = 150.0

        payload = ProtocolErrorPayload.model_validate(raw)

        self.assertEqual(payload.data.cap, 150.0)

    #Rechaza OVER_CAPACITY cuando falta data.spare.
    def test_over_capacity_requires_spare(self):
        with self.assertRaises(ValidationError):
            ProtocolErrorPayload.model_validate(
                valid_error_payload(
                    reason="OVER_CAPACITY",
                    code=409,
                )
            )

    #Acepta OVER_CAPACITY cuando incluye data.spare.
    def test_valid_over_capacity(self):
        raw = valid_error_payload(
            reason="OVER_CAPACITY",
            code=409,
        )
        raw["data"]["spare"] = 320.0

        payload = ProtocolErrorPayload.model_validate(raw)

        self.assertEqual(payload.data.spare, 320.0)

    #Rechaza combinaciones inválidas entre reason y code.
    def test_reject_wrong_reason_code_pair(self):
        with self.assertRaises(ValidationError):
            ProtocolErrorPayload.model_validate(
                valid_error_payload(
                    reason="CYCLE_UNKNOWN",
                    code=410,
                )
            )

    #Rechaza timestamps que no incluyen zona horaria.
    def test_reject_timestamp_without_timezone(self):
        with self.assertRaises(ValidationError):
            ProtocolErrorPayload.model_validate(
                valid_error_payload(
                    timestamp="2026-09-16T02:00:00",
                )
            )

    #Conserva data.target como referencia al mensaje original.
    def test_target_is_preserved(self):
        payload = ProtocolErrorPayload.model_validate(
            valid_error_payload()
        )

        self.assertEqual(
            payload.data.target,
            UUID("550e8400-e29b-41d4-a716-446655440002"),
        )


if __name__ == "__main__":
    unittest.main()