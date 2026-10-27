import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import (
    Cycle,
    MarketOffer,
    OutboundMessage,
    StrategyConfig,
)
from app.services.seller_offer import enqueue_seller_offer


class FakeWallet:
    async def get_balance(self):
        class Balance:
            kwh_available = Decimal("800")
        return Balance()


def test_creates_only_one_offer_and_outbound_message():
    now = datetime.now(timezone.utc)
    cycle_id = f"cycle-test-{uuid4()}"
    offer_id = f"{CITY_ID}-{cycle_id}"

    # Crear una estrategia exclusiva para este test.
    # No será ACTIVE ni PENDING, así no modifica la global.
    with Session(engine) as session:
        last_version = session.exec(
            select(StrategyConfig.version)
            .order_by(StrategyConfig.version.desc())
        ).first() or 0

        test_version = last_version + 1

        session.add(
            StrategyConfig(
                version=test_version,
                status="ARCHIVED",
                offer_price_markup=Decimal("1.20"),
                offer_quantity_fraction=Decimal("1.00"),
                bid_acceptance_policy="FIFO",
                offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
                span_seconds=60,
                created_at=now,
                created_by="pytest-e2-028",
            )
        )

        # Guardar primero la estrategia dentro de la transacción.
        session.flush()

        # Ahora el ciclo puede referenciar esa versión.
        session.add(
            Cycle(
                cycle_id=cycle_id,
                strategy_version=test_version,
                status_idpk=str(uuid4()),
                generation_capacity=Decimal("2000"),
                consumption=Decimal("1000"),
                generation_cost=Decimal("210"),
                market_opens_at=now - timedelta(seconds=10),
                market_last_offer_at=now + timedelta(minutes=5),
                market_closes_at=now + timedelta(minutes=6),
                valid_until=now + timedelta(minutes=11),
                status_payload={
                    "energy": {
                        "sellable": 1000,
                        "generationCost": 210,
                    },
                    "market": {
                        "buybackPrice": 220.50,
                    },
                },
                created_at=now,
            )
        )
        session.commit()

    try:
        first = asyncio.run(
            enqueue_seller_offer(
                cycle_id, wallet_client=FakeWallet()
            )
        )
        second = asyncio.run(
            enqueue_seller_offer(
                cycle_id, wallet_client=FakeWallet()
            )
        )

        assert first is True
        assert second is False

        with Session(engine) as session:
            offers = session.exec(
                select(MarketOffer).where(
                    MarketOffer.offer_id == offer_id
                )
            ).all()

            messages = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id,
                    OutboundMessage.message_type == "offer",
                )
            ).all()

            assert len(offers) == 1
            assert offers[0].initial_quantity == 800
            assert offers[0].price_per_energy == Decimal("252.00")

            assert len(messages) == 1
            assert messages[0].status == "PENDING"
            assert messages[0].dispatch_required is True
            assert messages[0].exchange_name == "market"
            assert messages[0].routing_key == f"market.offer.{CITY_ID}"

    finally:
        # Limpiar exclusivamente los datos creados por el test.
        with Session(engine) as session:
            messages = session.exec(
                select(OutboundMessage).where(
                    OutboundMessage.cycle_id == cycle_id
                )
            ).all()

            for message in messages:
                session.delete(message)

            offers = session.exec(
                select(MarketOffer).where(
                    MarketOffer.offer_id == offer_id
                )
            ).all()

            for offer in offers:
                session.delete(offer)

            cycle = session.get(Cycle, cycle_id)
            if cycle is not None:
                session.delete(cycle)

            session.flush()

            strategy = session.exec(
                select(StrategyConfig).where(
                    StrategyConfig.version == test_version
                )
            ).first()

            if strategy is not None:
                session.delete(strategy)

            session.commit()
