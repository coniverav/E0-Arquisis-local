"""E2-056: historial RF01 completo, aislado y sin exponer swapToken."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.database import engine
from app.main import app
from app.models import (
    Cycle, ImbalanceSettlement, LedgerEntry, MarketBid, MarketOffer,
    NegotiationReport, OutboundMessage, SwapRecord, TradeRecord,
)

client = TestClient(app)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def _cycle(session, *, cycle_id=None):
    cycle_id = cycle_id or f"e2056-{uuid4()}"
    session.add(Cycle(
        cycle_id=cycle_id, status_msg_id=str(uuid4()), status_idpk=str(uuid4()),
        status_payload={"energy": {"sellable": 150}, "market": {"buybackPrice": 210}},
        created_at=NOW, valid_until=NOW + timedelta(minutes=20),
        market_opens_at=NOW, market_last_offer_at=NOW + timedelta(minutes=8),
        market_last_bid_at=NOW + timedelta(minutes=9),
        market_closes_at=NOW + timedelta(minutes=10),
        budget_balance=Decimal("50"), energy_balance=Decimal("0"),
        last_sequence=2, last_operation_type="SWAP_TRANSPORT", last_operation_at=NOW,
    ))
    session.flush()
    return cycle_id


def test_cycle_history_404():
    response = client.get("/cycles/not-existing/history")
    assert response.status_code == 404


def test_cycle_history_empty_sections_and_status_statement():
    with Session(engine) as session:
        cycle_id = _cycle(session)
        session.commit()

    response = client.get(f"/cycles/{cycle_id}/history")
    assert response.status_code == 200, response.text
    history = response.json()
    assert history["cycleId"] == cycle_id
    assert history["statusStatement"]["energy"]["sellable"] == 150
    assert history["statusStatement"]["market"]["buybackPrice"] == 210
    assert history["funding"] == []
    assert history["offers"] == []
    assert history["offerRevisions"] == []
    assert history["bids"] == []
    assert history["swaps"] == []
    assert history["trades"] == []
    assert history["settlements"] == []
    assert history["negotiationReports"] == []
    assert history["lastOperation"] is None
    assert history["ledger"] == []


def test_cycle_history_includes_rf01_sections_and_redacts_tokens():
    with Session(engine) as session:
        cycle_id = _cycle(session)
        offer_id = f"KLD-{cycle_id}"
        bid_idpk = str(uuid4())
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=cycle_id, city_id="KLD", is_local=True,
            initial_quantity=150, remaining_quantity=100,
            price_per_energy=Decimal("225.25"), status="ACTIVE",
            published_at=NOW, last_updated_at=NOW + timedelta(minutes=1),
        ))
        session.flush()
        session.add(OutboundMessage(
            msg_id=str(uuid4()), idpk=str(uuid4()), message_type="offer-updated",
            cycle_id=cycle_id, payload={"data": {
                "offerId": offer_id, "remaining": 100, "pricePerEnergy": 225.25,
            }}, status="PUBLISHED", created_at=NOW,
            published_at=NOW + timedelta(minutes=1),
        ))
        session.add(MarketBid(
            idpk=bid_idpk, msg_id=str(uuid4()), cycle_id=cycle_id,
            direction="INCOMING", offer_id=offer_id, counterpart_city_id="COR",
            quantity=50, status="ACCEPTED", swap_id="swap-123",
            created_at=NOW, responded_at=NOW + timedelta(minutes=2),
        ))
        session.add(SwapRecord(
            idpk=str(uuid4()), swap_id="swap-123", cycle_id=cycle_id,
            role="PROPOSER", counterpart_city_id="COR",
            give_asset="KWH", give_amount=Decimal("50"),
            take_asset="CR", take_amount=Decimal("11262.50"),
            memo=offer_id, status="SETTLED", idempotency_key=str(uuid4()),
            swap_token="SUPER_SECRET_TOKEN_DO_NOT_EXPOSE",
            proposal_http_accepted_at=NOW + timedelta(minutes=1),
            created_at=NOW, settled_at=NOW + timedelta(minutes=4),
        ))
        session.add(TradeRecord(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id=cycle_id,
            swap_id="swap-123", offer_id=offer_id, seller="KLD", buyer="COR",
            role="SELLER", counterpart_city_id="COR", quantity=50,
            price_per_energy=Decimal("225.25"), energy_total=Decimal("11262.50"),
            transport_per_energy=Decimal("3"), transport_total=Decimal("150"),
            route_enabled=True, recorded_at=NOW + timedelta(minutes=4),
        ))
        session.add(ImbalanceSettlement(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id=cycle_id,
            settlement_id="settlement-123", kwh_held=0, deficit=0, coverage=0,
            price_per_energy=Decimal("210"), total=Decimal("0"),
            balance_after=Decimal("50"), recorded_at=NOW + timedelta(minutes=11),
        ))
        session.add(NegotiationReport(
            cycle_id=cycle_id, msg_id=str(uuid4()), idpk=str(uuid4()),
            status="PUBLISHED", budget_balance=Decimal("50"),
            energy_balance=Decimal("0"), created_at=NOW + timedelta(minutes=12),
        ))
        session.add(LedgerEntry(
            cycle_id=cycle_id, sequence_no=2, idpk=str(uuid4()),
            operation_type="SWAP_TRANSPORT", source_msg_id=str(uuid4()),
            budget_delta=Decimal("-150"), energy_delta=Decimal("0"),
            budget_after=Decimal("50"), energy_after=Decimal("0"),
            details={"swapId": "swap-123"}, applied_at=NOW + timedelta(minutes=4),
        ))
        session.add(LedgerEntry(
            cycle_id=cycle_id, sequence_no=1, idpk=str(uuid4()),
            operation_type="TRANSFER_IN", source_msg_id=str(uuid4()),
            budget_delta=Decimal("200"), energy_delta=Decimal("0"),
            budget_after=Decimal("200"), energy_after=Decimal("0"),
            details={"funding": {"balanceAfter": "200", "penalty": 0}},
            applied_at=NOW,
        ))
        session.commit()

    response = client.get(f"/cycles/{cycle_id}/history")
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["funding"]) == 1
    assert result["funding"][0]["details"]["funding"]["balanceAfter"] == "200"
    assert result["offers"][0]["offerId"] == offer_id
    assert result["offerRevisions"][0]["remaining"] == 100
    assert result["bids"][0]["status"] == "ACCEPTED"
    assert result["swaps"][0]["status"] == "SETTLED"
    assert result["swaps"][0]["proposalHttpAcceptedAt"] is not None
    assert result["trades"][0]["transportTotal"] == "150.00"
    assert result["settlements"][0]["settlementId"] == "settlement-123"
    assert result["negotiationReports"][0]["status"] == "PUBLISHED"
    assert [entry["sequence"] for entry in result["ledger"]] == [1, 2]
    assert result["lastOperation"]["operationType"] == "SWAP_TRANSPORT"
    assert "SUPER_SECRET_TOKEN_DO_NOT_EXPOSE" not in response.text
    assert "swapToken" not in response.text
    assert "idempotencyKey" not in response.text


def test_cycle_history_only_returns_requested_cycle():
    with Session(engine) as session:
        first = _cycle(session)
        second = _cycle(session)
        offer_id = f"only-{uuid4()}"
        session.add(MarketOffer(
            offer_id=offer_id, cycle_id=second, city_id="KLD",
            is_local=True, initial_quantity=5, remaining_quantity=5,
            price_per_energy=Decimal("10"), published_at=NOW, last_updated_at=NOW,
        ))
        session.flush()
        session.add(MarketBid(
            idpk=str(uuid4()), msg_id=str(uuid4()), cycle_id=second,
            direction="INCOMING", offer_id=offer_id,
            counterpart_city_id="COR", quantity=5, status="PENDING", created_at=NOW,
        ))
        session.commit()

    assert client.get(f"/cycles/{first}/history").json()["bids"] == []
    assert len(client.get(f"/cycles/{second}/history").json()["bids"]) == 1
