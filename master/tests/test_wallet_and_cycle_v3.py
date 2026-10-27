"""Pruebas para E2-019 a E2-023:
- Cliente Wallet REST autenticado con X-Api-Key (E2-019).
- Idempotency-Key durable en POST/accept/cancel (E2-020).
- Correlación de eventos swap-* por idpk y swapId (E2-021).
- Snapshots de balances wallet available/held y creditLine (E2-022).
- Adaptación del ciclo v3 con timestamps de central (E2-023).
"""

from datetime import datetime, timezone
from decimal import Decimal
import uuid
import pytest
from unittest.mock import AsyncMock, patch
import httpx
from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, SwapRecord, WalletBalanceSnapshot, MarketOffer, OfferReservation
from app.services.wallet_client import (
    WalletClient,
    WalletBalanceResponse,
    WalletSwapWriteResponse,
    WalletError,
    WalletRateLimitError,
    WalletTimeoutError,
)
from app.services.wallet_service import WalletService
from app.services.cycle_scheduler import configure_cycle_schedule
from tests.contracts_v3 import example


@pytest.mark.anyio
async def test_wallet_client_headers_and_auth():
    """Valida autenticación con X-Api-Key e Idempotency-Key (E2-019, E2-020)."""
    client = WalletClient(
        base_url="https://wallet.central.test",
        api_key="secret-api-key-123",
        timeout=5.0,
    )
    headers = client._get_headers(idempotency_key="idemp-key-uuid")
    assert headers["X-Api-Key"] == "secret-api-key-123"
    assert headers["Idempotency-Key"] == "idemp-key-uuid"
    assert headers["Content-Type"] == "application/json"

    # Verificar que los logs enmascaran la clave sensible
    masked = client._mask_headers(headers)
    assert masked["X-Api-Key"] == "***REDACTED***"


@pytest.mark.anyio
async def test_wallet_balance_uses_observed_client_and_v3_assets():
    """La oferta seller lee el saldo v3 sin perder la observabilidad de develop."""
    client = WalletClient(base_url="https://wallet.central.test", api_key="test-key")
    balance_response = httpx.Response(200, json=example("wallet-balance-response.json"))
    with patch("app.services.wallet_client.ObservedWalletAsyncClient") as observed:
        observed.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=balance_response
        )
        balance = await client.get_balance()

    observed.assert_called_once()
    observed.return_value.__aenter__.return_value.get.assert_awaited_once_with(
        "https://wallet.central.test/balance", headers=client._get_headers()
    )
    assert balance.city_id == "KLD"
    assert balance.kwh_available == Decimal("7300")
    assert balance.kwh_held == Decimal("0")
    assert balance.cr_available == Decimal("1791000.0")
    assert balance.cr_held == Decimal("0.0")
    assert balance.credit_line == Decimal("1200000.0")
    assert balance.timestamp == "2026-10-26T14:06:14.602Z"
    assert balance.last_settled_cycle == "cycle-9430"
    assert balance.balance_at_last_close_cr == Decimal("1150000.0")


@pytest.mark.anyio
async def test_wallet_balance_rejects_non_v3_shape():
    client = WalletClient(base_url="https://wallet.central.test", api_key="test-key")
    balance_response = httpx.Response(200, json={
        "cityId": "KLD",
        "kwh": {"available": 1, "held": 0},
        "cr": {"available": 1, "held": 0, "creditLine": 0},
    })
    with patch("app.services.wallet_client.ObservedWalletAsyncClient") as observed:
        observed.return_value.__aenter__.return_value.get = AsyncMock(
            return_value=balance_response
        )
        with pytest.raises(WalletError):
            await client.get_balance()


@pytest.mark.anyio
async def test_wallet_client_rate_limit_handling():
    """Valida detección de HTTP 429 y Retry-After (E2-019)."""
    client = WalletClient()
    mock_resp = httpx.Response(
        status_code=429,
        headers={"Retry-After": "30"},
        text="Rate limit exceeded",
    )
    with pytest.raises(WalletRateLimitError) as exc_info:
        client._handle_response_error(mock_resp)
    assert exc_info.value.retry_after == 30
    assert exc_info.value.status_code == 429


@pytest.mark.anyio
async def test_wallet_service_idempotent_swap_creation():
    """Valida que SwapRecord reuse la misma idempotency_key ante reintentos (E2-020)."""
    service = WalletService()
    idpk = str(uuid.uuid4())
    cycle_id = f"c-{uuid.uuid4().hex[:8]}"

    with Session(engine) as session:
        record1 = service.get_or_create_swap_record(
            session,
            idpk=idpk,
            cycle_id=cycle_id,
            role="PROPOSER",
            counterpart_city_id="ZAR",
            give_asset="KWH",
            give_amount=Decimal("100"),
            take_asset="CR",
            take_amount=Decimal("1500"),
        )
        first_key = record1.idempotency_key
        assert first_key is not None

        # Segunda llamada con el mismo idpk (replay o reintento)
        record2 = service.get_or_create_swap_record(
            session,
            idpk=idpk,
            cycle_id=cycle_id,
            role="PROPOSER",
            counterpart_city_id="ZAR",
            give_asset="KWH",
            give_amount=Decimal("100"),
            take_asset="CR",
            take_amount=Decimal("1500"),
        )
        assert record2.idempotency_key == first_key
        assert record2.id == record1.id


@pytest.mark.anyio
async def test_wallet_service_process_swap_settled_and_reservations():
    """Valida correlación de swap-settled y confirmación de reserva (E2-021, E2-022, AD4)."""
    service = WalletService()
    idpk = str(uuid.uuid4())
    swap_id = f"sw-{uuid.uuid4().hex[:8]}"
    offer_id = f"off-{uuid.uuid4().hex[:8]}"
    cycle_id = f"c-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        # Oferta local y reserva previa, consistentes con el swap oficial.
        offer = MarketOffer(
            offer_id=offer_id,
            cycle_id=cycle_id,
            city_id="KLD",
            is_local=True,
            initial_quantity=20000,
            remaining_quantity=20000,
            price_per_energy=Decimal("330.0"),
            status="ACTIVE",
            published_at=now,
            last_updated_at=now,
        )
        session.add(offer)
        reservation = OfferReservation(
            offer_id=offer_id,
            bid_idpk=idpk,
            quantity=7300,
            status="HOLD",
            created_at=now,
        )
        session.add(reservation)

        # Registro del swap
        record = service.get_or_create_swap_record(
            session,
            idpk=idpk,
            cycle_id=cycle_id,
            role="PROPOSER",
            counterpart_city_id="COR",
            give_asset="KWH",
            give_amount=Decimal("7300"),
            take_asset="CR",
            take_amount=Decimal("2409000"),
            memo=offer_id,
            swap_id=swap_id,
        )

        # Llega swap-settled con el balancesAfter oficial de v3.0.1.
        event_data = dict(example("swap-settled.json")["data"])
        event_data["idpk"] = idpk
        event_data["swapId"] = swap_id
        updated_record = service.process_swap_event(
            session,
            event_type="swap-settled",
            data=event_data,
        )

        assert updated_record.status == "SETTLED"
        assert updated_record.settled_at is not None

        # La reserva debe haber pasado a COMMITTED y remaining descontado
        session.refresh(reservation)
        session.refresh(offer)
        assert reservation.status == "COMMITTED"
        assert offer.remaining_quantity == 12700

        # Snapshot de balances verificado (E2-022)
        snapshot = session.exec(
            select(WalletBalanceSnapshot).where(
                WalletBalanceSnapshot.cycle_id == cycle_id,
                WalletBalanceSnapshot.source == "EVENT",
            )
        ).one()
        assert snapshot.kwh_available == Decimal("7300")
        assert snapshot.kwh_held == Decimal("0")
        assert snapshot.cr_available == Decimal("1791000.0")
        assert snapshot.cr_held == Decimal("0.0")
        assert snapshot.credit_line is None

        session.commit()


def test_record_balance_snapshot_rejects_lowercase_shape():
    """La forma E1 (`kwh`/`cr`) no debe guardarse silenciosamente en cero."""
    service = WalletService()
    with Session(engine) as session:
        with pytest.raises(ValueError):
            service.record_balance_snapshot(
                session,
                cycle_id="c-invalid",
                source="EVENT",
                balances_dict={
                    "kwh": {"available": 1, "held": 0},
                    "cr": {"available": 1, "held": 0},
                    "creditLine": 0,
                },
            )
        session.rollback()


def test_cycle_v3_timestamps_adaptation():
    """Valida adaptación de ciclo v3 a timestamps de la central (E2-023, G03, RF01)."""
    cycle = Cycle(
        cycle_id="c-2026-10-25-10",
        created_at=datetime.now(timezone.utc),
    )
    valid_until = datetime.fromisoformat("2026-10-25T20:20:00+00:00")
    market_times = {
        "opensAt": "2026-10-25T20:05:00Z",
        "lastOfferAt": "2026-10-25T20:10:00Z",
        "lastBidAt": "2026-10-25T20:14:00Z",
        "closesAt": "2026-10-25T20:15:00Z",
        "reportOpensAt": "2026-10-25T20:15:00Z",
    }

    configure_cycle_schedule(
        cycle,
        valid_until=valid_until,
        market_times=market_times,
    )

    assert cycle.valid_until == valid_until
    assert cycle.market_opens_at == datetime.fromisoformat("2026-10-25T20:05:00+00:00")
    assert cycle.market_last_offer_at == datetime.fromisoformat("2026-10-25T20:10:00+00:00")
    assert cycle.market_last_bid_at == datetime.fromisoformat("2026-10-25T20:14:00+00:00")
    assert cycle.market_closes_at == datetime.fromisoformat("2026-10-25T20:15:00+00:00")
    assert cycle.report_window_opens_at == datetime.fromisoformat("2026-10-25T20:15:00+00:00")
