"""E2-036: distance-table del comprador y cálculo de CR/kWh con penalización."""
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.database import engine, run_migrations
from app.main import app
from app.models import DistanceTable
from app.services.transport_pricing import calculate_transport


def setup_module():
    run_migrations()


def _offer(cycle_id, *, city="HGW", price="10.25"):
    message = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": "offer", "cycleId": cycle_id,
        "timestamp": "2026-10-20T10:00:00Z", "cityId": city,
        "data": {"offerId": str(uuid4()), "quantity": 10,
                 "pricePerEnergy": float(price)},
    }
    response = TestClient(app).post("/internal/audit/inbound/market", json=message)
    assert response.status_code == 200, response.text


def _distance(cycle_id, *, timestamp="2026-10-20T10:01:00Z", rate=0.0133,
              penalty=0.1200, enabled=True, distance=1500, city="HGW"):
    message = {
        "idpk": str(uuid4()), "msgId": str(uuid4()),
        "type": "distance-table", "sender": "central", "cycleId": cycle_id,
        "timestamp": timestamp,
        "data": {"distances": {city: {"distance": distance,
                                       "transportCost": rate, "enabled": enabled}},
                 "disabledRouteTransportCost": penalty},
    }
    response = TestClient(app).post("/internal/messages", json=message)
    assert response.status_code == 200, response.text
    return message


def _book(cycle_id):
    response = TestClient(app).get("/market/book", params={"cycleId": cycle_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_enabled_route_uses_kilometres_and_round2():
    cycle = f"cycle-{uuid4()}"
    _offer(cycle)
    _distance(cycle)
    book = _book(cycle)
    item = book["offers"][0]
    assert item["distanceMeters"] == "1500"
    assert item["routeEnabled"] is True
    assert Decimal(item["transportRate"]) == Decimal("0.0133")
    assert Decimal(item["transportPerEnergy"]) == Decimal("0.02")
    assert Decimal(item["effectivePricePerEnergy"]) == Decimal("10.27")
    assert Decimal(book["disabledRouteTransportCost"]) == Decimal("0.1200")


def test_disabled_route_charges_penalty_without_excluding_offer():
    cycle = f"cycle-{uuid4()}"
    _offer(cycle)
    _distance(cycle, enabled=False, rate=0.0010, penalty=0.1200)
    item = _book(cycle)["offers"][0]
    assert item["routeEnabled"] is False
    assert Decimal(item["transportRate"]) == Decimal("0.1200")
    assert Decimal(item["transportPerEnergy"]) == Decimal("0.18")
    assert Decimal(item["effectivePricePerEnergy"]) == Decimal("10.43")


def test_last_table_of_same_cycle_wins_and_old_table_cannot_override():
    cycle = f"cycle-{uuid4()}"
    _offer(cycle)
    _distance(cycle, timestamp="2026-10-20T10:02:00Z", rate=0.0200)
    _distance(cycle, timestamp="2026-10-20T10:01:00Z", rate=0.0900)
    assert Decimal(_book(cycle)["offers"][0]["transportPerEnergy"]) == Decimal("0.03")


def test_table_from_other_cycle_does_not_apply():
    cycle = f"cycle-{uuid4()}"
    _offer(cycle)
    _distance(f"cycle-{uuid4()}")
    item = _book(cycle)["offers"][0]
    assert item["transportPerEnergy"] is None
    assert item["effectivePricePerEnergy"] is None
    assert _book(cycle)["distanceTableTimestamp"] is None


def test_missing_seller_route_has_no_invented_price():
    cycle = f"cycle-{uuid4()}"
    _offer(cycle, city="TAR")
    _distance(cycle, city="HGW")
    item = _book(cycle)["offers"][0]
    assert item["routeEnabled"] is None
    assert item["effectivePricePerEnergy"] is None


def test_disabled_route_without_official_penalty_cannot_be_priced():
    table = DistanceTable(
        cycle_id="test", msg_id=str(uuid4()), idpk=str(uuid4()),
        distances={"HGW": {"distance": 1000, "transportCost": 0.01, "enabled": False}},
        disabled_route_transport_cost=None,
    )
    result = calculate_transport(table, seller_city="HGW", price_per_energy=Decimal("20.00"))
    assert result["routeEnabled"] is False
    assert result["transportPerEnergy"] is None
    assert result["effectivePricePerEnergy"] is None


def test_round_half_up_at_exact_half_cent():
    table = DistanceTable(
        cycle_id="test", msg_id=str(uuid4()), idpk=str(uuid4()),
        distances={"HGW": {"distance": 1000, "transportCost": 0.005, "enabled": True}},
    )
    result = calculate_transport(table, seller_city="HGW", price_per_energy=Decimal("2.00"))
    assert result["transportPerEnergy"] == Decimal("0.01")
    assert result["effectivePricePerEnergy"] == Decimal("2.01")


def test_distance_table_v3_fields_are_persisted_and_queryable():
    cycle = f"cycle-{uuid4()}"
    payload = _distance(cycle, enabled=False)
    with Session(engine) as session:
        table = session.exec(select(DistanceTable).where(DistanceTable.idpk == payload["idpk"])).one()
        assert table.cycle_id == cycle
        assert table.disabled_route_transport_cost == Decimal("0.1200")
    response = TestClient(app).get("/distance-table")
    assert response.status_code == 200
    # La consulta global puede contener versiones posteriores de otras pruebas;
    # el valor persistido en BD es el contrato de esta prueba.
