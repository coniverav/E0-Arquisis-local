"""Pruebas para los modelos v3 definidos en E2-018."""

from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from app.database import engine
from app.models import (
    MarketOffer,
    OfferReservation,
    MarketBid,
    SwapRecord,
    WalletBalanceSnapshot,
    StrategyConfig,
    InboundMessage,
    OutboundMessage,
)


def test_market_offer_and_reservations():
    """Valida la creación de ofertas y reservas persistidas para serialización AD4."""
    offer_id = f"off-{uuid.uuid4().hex[:8]}"
    cycle_id = "c-2026-10-25-01"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        offer = MarketOffer(
            offer_id=offer_id,
            cycle_id=cycle_id,
            city_id="KLD",
            is_local=True,
            initial_quantity=1000,
            remaining_quantity=1000,
            price_per_energy=Decimal("15.5000"),
            status="ACTIVE",
            published_at=now,
            last_updated_at=now,
        )
        session.add(offer)
        session.commit()

        # Agregar reserva asociada
        bid_idpk = str(uuid.uuid4())
        reservation = OfferReservation(
            offer_id=offer.offer_id,
            bid_idpk=bid_idpk,
            quantity=200,
            status="HOLD",
            created_at=now,
        )
        session.add(reservation)
        session.commit()

        # Consultar la oferta con sus reservas
        loaded_offer = session.exec(
            select(MarketOffer).where(MarketOffer.offer_id == offer_id)
        ).one()
        assert loaded_offer.offer_id == offer_id
        assert loaded_offer.price_per_energy == Decimal("15.5000")
        assert len(loaded_offer.reservations) == 1
        assert loaded_offer.reservations[0].bid_idpk == bid_idpk
        assert loaded_offer.reservations[0].quantity == 200
        assert loaded_offer.reservations[0].status == "HOLD"


def test_market_bids_lifecycle():
    """Valida registro y estados de un bid v3."""
    offer_id = f"off-{uuid.uuid4().hex[:8]}"
    bid_idpk = str(uuid.uuid4())
    msg_id = str(uuid.uuid4())
    cycle_id = "c-2026-10-25-01"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        offer = MarketOffer(
            offer_id=offer_id,
            cycle_id=cycle_id,
            city_id="ZAR",
            is_local=False,
            initial_quantity=500,
            remaining_quantity=500,
            price_per_energy=Decimal("12.0000"),
            status="ACTIVE",
            published_at=now,
            last_updated_at=now,
        )
        session.add(offer)
        session.commit()

        bid = MarketBid(
            idpk=bid_idpk,
            msg_id=msg_id,
            cycle_id=cycle_id,
            direction="OUTGOING",
            offer_id=offer_id,
            counterpart_city_id="ZAR",
            quantity=150,
            status="PENDING",
            created_at=now,
        )
        session.add(bid)
        session.commit()

        loaded_bid = session.exec(
            select(MarketBid).where(MarketBid.idpk == bid_idpk)
        ).one()
        assert loaded_bid.direction == "OUTGOING"
        assert loaded_bid.status == "PENDING"
        assert loaded_bid.quantity == 150
        assert loaded_bid.offer.city_id == "ZAR"


def test_swap_record_idempotency_and_correlation():
    """Valida SwapRecord con idempotency_key durable y correlación con idpk (AD5)."""
    idpk = str(uuid.uuid4())
    idempotency_key = str(uuid.uuid4())
    cycle_id = "c-2026-10-25-01"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        swap = SwapRecord(
            idpk=idpk,
            swap_id="sw-999888",
            cycle_id=cycle_id,
            role="PROPOSER",
            counterpart_city_id="VLD",
            give_asset="KWH",
            give_amount=Decimal("300.0000"),
            take_asset="CR",
            take_amount=Decimal("4500.0000"),
            memo="off-local-01",
            status="SUBMITTED",
            idempotency_key=idempotency_key,
            created_at=now,
        )
        session.add(swap)
        session.commit()

        loaded = session.exec(
            select(SwapRecord).where(SwapRecord.idpk == idpk)
        ).one()
        assert loaded.swap_id == "sw-999888"
        assert loaded.give_asset == "KWH"
        assert loaded.give_amount == Decimal("300.0000")
        assert loaded.idempotency_key == idempotency_key


def test_wallet_balance_snapshot():
    """Valida el almacenamiento de disponibles, retenidos y creditLine de la wallet (E2-022)."""
    now = datetime.now(timezone.utc)
    cycle_id = f"c-{uuid.uuid4().hex[:8]}"

    with Session(engine) as session:
        snapshot = WalletBalanceSnapshot(
            cycle_id=cycle_id,
            source="REST_SYNC",
            kwh_available=Decimal("1250.0000"),
            kwh_held=Decimal("200.0000"),
            cr_available=Decimal("50000.5000"),
            cr_held=Decimal("1500.0000"),
            credit_line=Decimal("10000.0000"),
            recorded_at=now,
        )
        session.add(snapshot)
        session.commit()

        loaded = session.exec(
            select(WalletBalanceSnapshot).where(
                WalletBalanceSnapshot.cycle_id == cycle_id
            )
        ).one()
        assert loaded.kwh_available == Decimal("1250.0000")
        assert loaded.kwh_held == Decimal("200.0000")
        assert loaded.cr_available == Decimal("50000.5000")
        assert loaded.credit_line == Decimal("10000.0000")


def test_strategy_config_versioning():
    """Valida versionamiento de estrategias sin redeploy (AD6, RF04)."""
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        # Obtener máxima versión existente para no colisionar en ejecuciones consecutivas
        max_v = session.exec(
            select(StrategyConfig.version).order_by(StrategyConfig.version.desc())
        ).first() or 0

        v1 = max_v + 1
        v2 = max_v + 2

        strat1 = StrategyConfig(
            version=v1,
            status="ACTIVE",
            offer_price_markup=Decimal("1.2500"),
            offer_quantity_fraction=Decimal("0.9000"),
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=60,
            effective_from_cycle_id="c-2026-10-25-01",
            created_at=now,
            created_by="operator",
        )
        session.add(strat1)

        strat2 = StrategyConfig(
            version=v2,
            status="PENDING",
            offer_price_markup=Decimal("1.1000"),
            offer_quantity_fraction=Decimal("1.0000"),
            bid_acceptance_policy="FIFO",
            offer_selection_policy="CHEAPEST_EFFECTIVE_PRICE",
            span_seconds=120,
            effective_from_cycle_id="c-2026-10-25-02",
            created_at=now,
            created_by="operator",
        )
        session.add(strat2)
        session.commit()

        active = session.exec(
            select(StrategyConfig).where(StrategyConfig.version == v1)
        ).one()
        assert active.status == "ACTIVE"
        assert active.span_seconds == 60

        pending = session.exec(
            select(StrategyConfig).where(StrategyConfig.version == v2)
        ).one()
        assert pending.status == "PENDING"
        assert pending.span_seconds == 120
