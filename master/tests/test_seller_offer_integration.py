"""E2-028: la oferta seller nace de un `status-statement` oficial v3.0.1.

El test no fabrica el payload de estado: envía el ejemplo oficial por el
ingress real (`POST /internal/messages`), deja que el backend cree el ciclo y
asigne su estrategia AD6, y recién entonces ejecuta `enqueue_seller_offer`.
La wallet se simula en el borde HTTP con el fixture oficial de `GET /balance`,
de modo que también se ejerce el parser v3 real.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine, run_migrations
from app.main import app
from app.models import (
    Cycle,
    InboundMessage,
    LedgerEntry,
    MarketOffer,
    OutboundMessage,
    ProcessedIdpk,
    StrategyConfig,
)
from app.services.seller_offer import enqueue_seller_offer
from app.services.strategy_config import create_pending_strategy
from app.services.wallet_client import WalletClient
from tests.contracts_v3 import example, validate


CENT = Decimal("0.01")
OFFICIAL_WALLET_KWH = Decimal("7300")


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _official_status(
    *,
    cycle_id: str,
    now: datetime,
    generation_capacity: Decimal,
    consumption: Decimal,
    sellable: int,
) -> dict:
    """Ejemplo oficial v3.0.1 con tiempos y posición física de este test."""

    payload = deepcopy(example("status-statement.json"))
    payload["idpk"] = str(uuid4())
    payload["msgId"] = str(uuid4())
    payload["timestamp"] = _iso(now)
    payload["cycleId"] = cycle_id

    energy = payload["data"]["energy"]
    energy["generationCapacity"] = float(generation_capacity)
    energy["consumption"] = float(consumption)
    energy["sellable"] = sellable

    market = payload["data"]["market"]
    market["opensAt"] = _iso(now - timedelta(seconds=10))
    market["lastOfferAt"] = _iso(now + timedelta(minutes=5))
    market["lastBidAt"] = _iso(now + timedelta(minutes=5))
    market["closesAt"] = _iso(now + timedelta(minutes=6))
    market["reportOpensAt"] = _iso(now + timedelta(minutes=6))
    payload["data"]["validUntil"] = _iso(now + timedelta(minutes=7))

    validate("amqp/status-statement", payload)
    return payload


class Scenario:
    """Ciclo creado por el ingress y limpieza acotada a sus propias filas."""

    def __init__(
        self,
        *,
        generation_capacity: Decimal = Decimal("20000"),
        consumption: Decimal = Decimal("10000"),
        sellable: int = 10000,
    ):
        self.now = datetime.now(timezone.utc)
        self.cycle_id = f"cycle-e2028-{uuid4()}"
        self.offer_id = f"{CITY_ID}-{self.cycle_id}"
        self.envelope = _official_status(
            cycle_id=self.cycle_id,
            now=self.now,
            generation_capacity=generation_capacity,
            consumption=consumption,
            sellable=sellable,
        )

    def __enter__(self):
        run_migrations()
        self.client = TestClient(app)

        response = self.client.post("/internal/messages", json=self.envelope)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ok"

        with Session(engine) as session:
            cycle = session.get(Cycle, self.cycle_id)
            assert cycle is not None
            assert cycle.status_idpk == self.envelope["idpk"]
            assert cycle.strategy_version is not None
            self.strategy = session.exec(
                select(StrategyConfig).where(
                    StrategyConfig.version == cycle.strategy_version
                )
            ).one()

        return self

    def __exit__(self, *exc_info):
        with Session(engine) as session:
            session.exec(
                delete(OutboundMessage).where(
                    OutboundMessage.cycle_id == self.cycle_id
                )
            )
            session.exec(
                delete(MarketOffer).where(
                    MarketOffer.cycle_id == self.cycle_id
                )
            )
            session.exec(
                delete(LedgerEntry).where(
                    LedgerEntry.cycle_id == self.cycle_id
                )
            )
            session.exec(
                delete(InboundMessage).where(
                    InboundMessage.idpk == self.envelope["idpk"]
                )
            )
            session.exec(
                delete(ProcessedIdpk).where(
                    ProcessedIdpk.idpk == self.envelope["idpk"]
                )
            )
            session.exec(
                delete(Cycle).where(Cycle.cycle_id == self.cycle_id)
            )
            session.commit()

    @property
    def expected_quantity(self) -> int:
        available = min(
            Decimal(self.envelope["data"]["energy"]["sellable"]),
            OFFICIAL_WALLET_KWH,
        )
        return int(available * self.strategy.offer_quantity_fraction)

    @property
    def expected_price(self) -> Decimal:
        generation_cost = Decimal(
            str(self.envelope["data"]["energy"]["generationCost"])
        )
        buyback = Decimal(
            str(self.envelope["data"]["market"]["buybackPrice"])
        )
        marked_up = (generation_cost * self.strategy.offer_price_markup).quantize(
            CENT
        )
        return max(buyback, marked_up).quantize(CENT)

    def run(self):
        """Ejecuta la oferta con un WalletClient real y HTTP simulado."""

        wallet_response = httpx.Response(
            200, json=example("wallet-balance-response.json")
        )
        wallet = WalletClient(
            base_url="https://wallet.central.test", api_key="test-api-key"
        )

        with patch(
            "app.services.wallet_client.ObservedWalletAsyncClient"
        ) as observed:
            observed.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=wallet_response
            )
            first = asyncio.run(
                enqueue_seller_offer(self.cycle_id, wallet_client=wallet)
            )
            second = asyncio.run(
                enqueue_seller_offer(self.cycle_id, wallet_client=wallet)
            )

        return first, second

    def offers(self):
        with Session(engine) as session:
            return session.exec(
                select(MarketOffer).where(
                    MarketOffer.offer_id == self.offer_id
                )
            ).all()

    def outbound(self):
        with Session(engine) as session:
            return session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id == self.cycle_id,
                    OutboundMessage.message_type == "offer",
                )
            ).all()


def test_creates_one_offer_from_official_status_statement():
    with Scenario() as scenario:
        first, second = scenario.run()

        assert first is True
        assert second is False

        offers = scenario.offers()
        assert len(offers) == 1
        assert offers[0].cycle_id == scenario.cycle_id
        assert offers[0].city_id == CITY_ID
        assert offers[0].initial_quantity == scenario.expected_quantity
        assert offers[0].remaining_quantity == scenario.expected_quantity
        assert offers[0].price_per_energy == scenario.expected_price
        assert offers[0].price_per_energy >= Decimal(
            str(scenario.envelope["data"]["market"]["buybackPrice"])
        )

        messages = scenario.outbound()
        assert len(messages) == 1

        message = messages[0]
        assert message.status == "PENDING"
        assert message.dispatch_required is True
        assert message.exchange_name == "market"
        assert message.routing_key == f"market.offer.{CITY_ID}"
        assert message.available_at == datetime.fromisoformat(
            scenario.envelope["data"]["market"]["opensAt"].replace(
                "Z", "+00:00"
            )
        )
        assert message.expires_at == datetime.fromisoformat(
            scenario.envelope["data"]["market"]["lastOfferAt"].replace(
                "Z", "+00:00"
            )
        )

        # El mensaje del outbox es exactamente un `offer` v3.0.1.
        validate("amqp/offer", message.payload)
        assert message.payload["data"]["offerId"] == scenario.offer_id
        assert message.payload["data"]["quantity"] == scenario.expected_quantity
        assert message.payload["data"]["pricePerEnergy"] == float(
            scenario.expected_price
        )
        assert message.payload["cycleId"] == scenario.cycle_id
        assert message.payload["cityId"] == CITY_ID
        assert message.payload["idpk"] != message.payload["msgId"]


def test_direct_price_and_quantity_are_published_from_cycle_strategy():
    with Session(engine) as session:
        pending = create_pending_strategy(
            session,
            offer_price_markup=Decimal("1.2000"),
            offer_quantity_fraction=Decimal("1.0000"),
            offer_price_override=Decimal("1.00"),
            offer_quantity_cap=500,
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=60,
        )
        # La edición antecede a opensAt oficial del siguiente ciclo.
        pending.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.add(pending)
        session.commit()
    with Scenario() as scenario:
        assert scenario.strategy.offer_price_override == Decimal("1.00")
        assert scenario.strategy.offer_quantity_cap == 500
        assert Decimal(str(scenario.envelope["data"]["market"]["buybackPrice"])) > Decimal("1.00")
        first, second = scenario.run()
        assert (first, second) == (True, False)
        assert len(scenario.offers()) == 1
        assert scenario.offers()[0].price_per_energy == Decimal("1.00")
        assert scenario.offers()[0].initial_quantity == 500
        message = scenario.outbound()[0].payload
        validate("amqp/offer", message)
        assert message["data"]["pricePerEnergy"] == 1.0
        assert message["data"]["quantity"] == 500


def test_does_not_publish_offer_below_min_bid():
    # El ciclo sí tiene energía vendible (sellable > 0), pero menos que el
    # `minBid` oficial: un bid válido nunca podría tomarla.
    with Scenario(
        generation_capacity=Decimal("10100"),
        consumption=Decimal("10000"),
        sellable=100,
    ) as scenario:
        assert scenario.expected_quantity < int(
            scenario.envelope["data"]["market"]["minBid"]
        )

        first, second = scenario.run()

        assert first is False
        assert second is False
        assert scenario.offers() == []
        assert scenario.outbound() == []


def test_zero_sellable_is_not_eligible_even_before_reading_wallet():
    with Scenario(
        generation_capacity=Decimal("9000"),
        consumption=Decimal("10000"),
        sellable=0,
    ) as scenario:
        # `_eligible` descarta el ciclo antes de consultar la wallet.
        wallet = WalletClient(
            base_url="https://wallet.central.test", api_key="test-api-key"
        )
        with patch(
            "app.services.wallet_client.ObservedWalletAsyncClient"
        ) as observed:
            result = asyncio.run(
                enqueue_seller_offer(scenario.cycle_id, wallet_client=wallet)
            )
            observed.assert_not_called()

        assert result is False
        assert scenario.offers() == []
        assert scenario.outbound() == []


def test_offer_payload_fixture_is_contract_valid():
    """Guardia mínima: el ejemplo oficial sigue siendo válido tras el import."""

    payload = example("offer.json")
    validate("amqp/offer", payload)
    assert payload["data"]["offerId"]
    assert isinstance(payload["data"]["quantity"], int)
    assert isinstance(payload["data"]["pricePerEnergy"], (int, float))
