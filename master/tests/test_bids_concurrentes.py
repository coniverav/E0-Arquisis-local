"""E2-069 / AD4: integración real de ingress + PostgreSQL + Wallet simulada.

Requiere base aislada energyshark_test_* (conftest.py la limpia).
No se conecta a RabbitMQ ni a Wallet Central. Las escrituras de prueba
se ejecutan contra PostgreSQL real y FastAPI real mediante TestClient.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from threading import Barrier
from time import perf_counter, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.main import app
from app.models import Cycle, MarketBid, MarketOffer, OfferReservation, OutboundMessage, SwapRecord
from app.services.seller_swap_dispatch import dispatch_swap_proposals
from app.services.wallet_client import WalletError, WalletSwapWriteResponse
from app.services.wallet_service import WalletService

TEST_MODE = "SIMULACION_LOCAL_SIN_CENTRAL"
INCOMING_BUYERS = ("COR", "HGW", "AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH")


def _context(*, quantity: int = 100, min_bid: int = 1) -> dict:
    """Cada escenario usa un ciclo/oferta únicos y una ventana de mercado abierta."""
    now = datetime.now(timezone.utc)
    cid = f"e2069-{uuid4()}"
    oid = f"{CITY_ID}-{cid}"
    with Session(engine) as session:
        session.add(Cycle(
            cycle_id=cid, created_at=now, status_idpk=str(uuid4()),
            market_opens_at=now - timedelta(minutes=1),
            market_last_bid_at=now + timedelta(minutes=10),
            market_last_offer_at=now + timedelta(minutes=11),
            market_closes_at=now + timedelta(minutes=12),
            valid_until=now + timedelta(minutes=13),
            status_payload={"market": {"minBid": min_bid}},
        ))
        session.add(MarketOffer(
            offer_id=oid, cycle_id=cid, city_id=CITY_ID, is_local=True,
            initial_quantity=quantity, remaining_quantity=quantity,
            price_per_energy=Decimal("4.25"), status="ACTIVE",
            published_at=now, last_updated_at=now,
        ))
        session.commit()
    return {"cycle": cid, "offer": oid, "capacity": quantity, "now": now}


def _bid(ctx: dict, *, buyer: str, amount: int, key: str | None = None) -> dict:
    assert buyer != CITY_ID, "El comprador de prueba debe ser otra ciudad"
    return {
        "idpk": key or str(uuid4()),
        "msgId": str(uuid4()),
        "type": "bid",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cityId": buyer,
        "cycleId": ctx["cycle"],
        "data": {"offerId": ctx["offer"], "quantity": amount},
    }


def _post(message: dict) -> dict:
    # El camino real incluye inbox, idempotencia, validación, fila bloqueada
    # y outbox. Una instancia de TestClient por hilo evita compartir estado.
    with TestClient(app) as client:
        result = client.post("/internal/messages", json=message)
    assert result.status_code == 200, f"ingress HTTP {result.status_code}: {result.text}"
    data = result.json()
    assert data["status"] in {"ok", "duplicate"}, data
    return data


def _burst(messages: list[dict], *, gap_seconds: float = .040) -> list[dict]:
    """Inicio coordinado; cada envío empieza 40 ms después del anterior.

    El sistema operativo no garantiza el orden de adquisición de los locks;
    por eso las aserciones nunca asumen cuál ciudad gana la carrera.
    """
    barrier = Barrier(len(messages))
    t0 = perf_counter()

    def send(index: int, msg: dict) -> dict:
        barrier.wait(timeout=20)
        sleep(gap_seconds * index)
        started_at = perf_counter()
        response = _post(msg)
        return {"idpk": msg["idpk"], "city": msg["cityId"],
                "requested": msg["data"]["quantity"],
                "offset_ms": round((started_at - t0) * 1000, 2),
                "result": response["status"]}

    with ThreadPoolExecutor(max_workers=len(messages)) as pool:
        futures = [pool.submit(send, idx, msg) for idx, msg in enumerate(messages)]
        return [future.result(timeout=75) for future in futures]


def _state(ctx: dict) -> dict:
    with Session(engine) as session:
        offer = session.exec(select(MarketOffer).where(
            MarketOffer.offer_id == ctx["offer"])).one()
        bids = list(session.exec(select(MarketBid).where(
            MarketBid.cycle_id == ctx["cycle"],
            MarketBid.offer_id == ctx["offer"],
            MarketBid.direction == "INCOMING")).all())
        reserves = list(session.exec(select(OfferReservation).where(
            OfferReservation.offer_id == ctx["offer"])).all())
        swaps = list(session.exec(select(SwapRecord).where(
            SwapRecord.cycle_id == ctx["cycle"],
            SwapRecord.role == "PROPOSER", SwapRecord.memo == ctx["offer"])).all())
        responses = list(session.exec(select(OutboundMessage).where(
            OutboundMessage.cycle_id == ctx["cycle"],
            OutboundMessage.message_type.in_(["bid-rejected", "bid-accepted"]))).all())
        return {
            "offer_remaining": offer.remaining_quantity,
            "bids": [{"idpk": b.idpk, "msgId": b.msg_id, "quantity": b.quantity,
                      "status": b.status, "buyer": b.counterpart_city_id,
                      "reason": b.rejection_reason, "available": b.available_on_rejection}
                     for b in bids],
            "reserves": [{"bid": r.bid_idpk, "quantity": r.quantity,
                          "status": r.status} for r in reserves],
            "swaps": [{"idpk": s.idpk, "key": s.idempotency_key,
                       "quantity": int(s.give_amount), "status": s.status,
                       "give_asset": s.give_asset} for s in swaps],
            "responses": [{"type": m.message_type, "target": m.target_msg_id,
                           "route": m.routing_key, "payload": m.payload}
                          for m in responses],
        }


def _assert_ad4(ctx: dict, state: dict, *, expected_rejected_available: int | None = None) -> None:
    """Verifica capacidad, unicidad y correspondencia outbox ↔ bid."""
    bids, reservations, swaps, responses = (
        state["bids"], state["reserves"], state["swaps"], state["responses"])
    active = [r for r in reservations if r["status"] == "HOLD"]
    accepted_bids = [b for b in bids if b["status"] == "RESERVED"]
    rejected_bids = [b for b in bids if b["status"] == "REJECTED"]
    total_hold = sum(r["quantity"] for r in active)
    assert state["offer_remaining"] >= 0
    assert total_hold <= state["offer_remaining"], "SOBREVENTA_AD4"
    assert len({r["bid"] for r in reservations}) == len(reservations), "HOLD_DUPLICADO"
    assert len({s["idpk"] for s in swaps}) == len(swaps), "SWAP_DUPLICADO"
    assert len(active) == len(swaps) == len(accepted_bids)
    assert {r["bid"] for r in active} == {s["idpk"] for s in swaps}
    assert {s["idpk"] for s in swaps} == {b["idpk"] for b in accepted_bids}
    assert all(s["key"] == s["idpk"] and s["give_asset"] == "KWH" for s in swaps)
    assert not [r for r in responses if r["type"] == "bid-accepted"], (
        "No hay swap-proposed: no se puede publicar bid-accepted todavía")
    assert len([r for r in responses if r["type"] == "bid-rejected"]) == len(rejected_bids)
    for bid in rejected_bids:
        matches = [r for r in responses if r["type"] == "bid-rejected"
                   and r["target"] == bid["msgId"]]
        assert len(matches) == 1, "FALTA_RECHAZO_O_HAY_DUPLICADO"
        reply = matches[0]
        assert reply["route"] == f"city.{bid['buyer']}"
        assert reply["payload"]["reason"] == "QUANTITY_UNAVAILABLE"
        assert reply["payload"]["code"] == 409
        assert reply["payload"]["data"]["requested"] == bid["quantity"]
        assert reply["payload"]["data"]["target"] == bid["msgId"]
        assert type(reply["payload"]["data"]["available"]) is int
        assert reply["payload"]["data"]["available"] == bid["available"]
        assert 0 <= bid["available"] < bid["quantity"]
        if expected_rejected_available is not None:
            assert bid["available"] == expected_rejected_available, (
                f"available incorrecto: {bid['available']} != {expected_rejected_available}")


def _evidence(name: str, ctx: dict, state: dict, *, attempts: list | None = None,
              other: dict | None = None) -> None:
    dest = os.environ.get("E2069_REPORT_DIR")
    if not dest:
        return
    folder = Path(dest)
    folder.mkdir(parents=True, exist_ok=True)
    summary = {
        "mode": TEST_MODE,
        "result": "PASS",
        "case": name,
        "cycleId": ctx["cycle"],
        "offerId": ctx["offer"],
        "capacity": ctx["capacity"],
        "remaining": state["offer_remaining"],
        "total_holds": sum(r["quantity"] for r in state["reserves"] if r["status"] == "HOLD"),
        "reserved_bids": sum(b["status"] == "RESERVED" for b in state["bids"]),
        "rejected_bids": sum(b["status"] == "REJECTED" for b in state["bids"]),
        "available_rejections": [b["available"] for b in state["bids"] if b["status"] == "REJECTED"],
        "attempts": attempts or [],
        "details": other or {},
    }
    # No contiene tokens de acceso, API keys ni conexiones reales.
    (folder / f"{name}_{uuid4().hex[:8]}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _CapacityWallet:
    """Wallet fake: limita los KWH de todas las propuestas enviadas.

    No afirma nada sobre la Wallet Central: solo verifica que el vendedor no
    genere un POST por encima de su inventario ofrecido en el test.
    """

    def __init__(self, capacity: int):
        self.available = capacity
        self.calls = []
        self.insufficient_asset = 0

    async def propose_swap(self, **payload):
        amount = int(payload["give_amount"])
        assert payload["give_asset"] == "KWH"
        self.calls.append({"idempotency_key": payload["idempotency_key"],
                           "amount": amount, "memo": payload["memo"]})
        if amount > self.available:
            self.insufficient_asset += 1
            raise WalletError("INSUFFICIENT_ASSET:KWH", status_code=409)
        self.available -= amount
        return WalletSwapWriteResponse(
            status_code=202, data={}, idempotency_key=payload["idempotency_key"],
            accepted_for_processing=True,
        )


def test_40ms_dos_compradores_80_mas_80_sin_doble_hold() -> None:
    ctx = _context()
    messages = [_bid(ctx, buyer=INCOMING_BUYERS[i], amount=80) for i in range(2)]
    calls = _burst(messages)
    state = _state(ctx)
    _assert_ad4(ctx, state, expected_rejected_available=20)
    assert [b["status"] for b in state["bids"]].count("RESERVED") == 1
    assert [b["status"] for b in state["bids"]].count("REJECTED") == 1
    assert sum(r["quantity"] for r in state["reserves"]) == 80
    _evidence("40ms_80_mas_80", ctx, state, attempts=calls)


def test_rafaga_40ms_con_8_compradores_y_desborde_repetido() -> None:
    rounds = int(os.environ.get("E2069_ROUNDS", "4"))
    assert 1 <= rounds <= 20, "E2069_ROUNDS debe estar entre 1 y 20"
    for round_number in range(rounds):
        ctx = _context()
        messages = [_bid(ctx, buyer=INCOMING_BUYERS[i], amount=60) for i in range(8)]
        calls = _burst(messages)
        state = _state(ctx)
        _assert_ad4(ctx, state, expected_rejected_available=40)
        assert len(state["bids"]) == 8
        assert sum(b["status"] == "RESERVED" for b in state["bids"]) == 1
        assert sum(b["status"] == "REJECTED" for b in state["bids"]) == 7
        _evidence(f"rafaga_40ms_ronda_{round_number + 1}", ctx, state, attempts=calls)


def test_cupo_agotado_disponibilidad_cero() -> None:
    ctx = _context()
    first = _bid(ctx, buyer="COR", amount=60)
    second = _bid(ctx, buyer="HGW", amount=40)
    assert _post(first)["status"] == "ok"
    assert _post(second)["status"] == "ok"
    attempts = _burst([_bid(ctx, buyer="AAA", amount=10),
                       _bid(ctx, buyer="BBB", amount=10),
                       _bid(ctx, buyer="CCC", amount=10)])
    state = _state(ctx)
    _assert_ad4(ctx, state, expected_rejected_available=0)
    assert len(state["reserves"]) == len(state["swaps"]) == 2
    assert sum(r["quantity"] for r in state["reserves"]) == 100
    _evidence("cupo_agotado_available_0", ctx, state, attempts=attempts)


def test_doble_entrega_mismo_idpk_40ms_no_duplica() -> None:
    ctx = _context()
    first = _bid(ctx, buyer="COR", amount=70)
    second = dict(first, msgId=str(uuid4()), timestamp=datetime.now(timezone.utc).isoformat())
    calls = _burst([first, second])
    assert {x["result"] for x in calls} == {"ok", "duplicate"}
    state = _state(ctx)
    _assert_ad4(ctx, state)
    assert len(state["bids"]) == len(state["reserves"]) == len(state["swaps"]) == 1
    assert state["reserves"][0]["quantity"] == 70
    _evidence("idpk_duplicado_40ms", ctx, state, attempts=calls)


def test_wallet_fake_no_hay_insufficient_asset_ni_post_duplicado() -> None:
    ctx = _context()
    _burst([_bid(ctx, buyer="COR", amount=80),
            _bid(ctx, buyer="HGW", amount=80)])
    wallet = _CapacityWallet(ctx["capacity"])
    first = asyncio.run(dispatch_swap_proposals(limit=10, client=wallet))
    second = asyncio.run(dispatch_swap_proposals(limit=10, client=wallet))
    state = _state(ctx)
    _assert_ad4(ctx, state, expected_rejected_available=20)
    assert first["accepted_202"] == 1
    assert second["claimed"] == 0
    assert wallet.insufficient_asset == 0
    assert len(wallet.calls) == 1 and wallet.calls[0]["amount"] == 80
    assert state["swaps"][0]["status"] == "SUBMITTED"  # 202 no confirma swap-proposed
    _evidence("wallet_fake_sin_insufficient_asset", ctx, state,
              other={"wallet_mock_posts": len(wallet.calls),
                     "wallet_mock_insufficient_asset": wallet.insufficient_asset,
                     "http_202_no_es_settlement": True})


def test_commit_recalcula_remaining_sin_doble_consumo() -> None:
    ctx = _context()
    winner = _bid(ctx, buyer="COR", amount=60)
    assert _post(winner)["status"] == "ok"
    with Session(engine) as session:
        service = WalletService()
        for _ in range(2):
            service._update_offer_and_reservation(
                session, offer_id=ctx["offer"], bid_idpk=winner["idpk"], action="COMMIT")
        session.commit()
    # remaining = 40 (una sola vez). La siguiente solicitud no se debe aceptar.
    assert _post(_bid(ctx, buyer="HGW", amount=41))["status"] == "ok"
    assert _post(_bid(ctx, buyer="AAA", amount=40))["status"] == "ok"
    state = _state(ctx)
    assert state["offer_remaining"] == 40, "SETTLEMENT_DESCONTADO_DOS_VECES"
    assert sum(r["quantity"] for r in state["reserves"] if r["status"] == "HOLD") == 40
    denied = [b for b in state["bids"] if b["status"] == "REJECTED"]
    assert len(denied) == 1 and denied[0]["available"] == 40
    assert len([r for r in state["reserves"] if r["status"] == "COMMITTED"]) == 1
    # El tester AD4 estándar espera que cada SwapRecord tenga HOLD; después
    # de settlement ya existe COMMITTED, que deliberadamente se audita aparte.
    assert len({r["bid"] for r in state["reserves"]}) == len(state["reserves"])
    assert len({s["idpk"] for s in state["swaps"]}) == len(state["swaps"])
    _evidence("commit_remaining_idempotente", ctx, state)


def test_reserva_y_swap_se_revierten_juntos_ante_rollback() -> None:
    ctx = _context()
    key = str(uuid4())
    with Session(engine) as session:
        session.add(MarketBid(
            idpk=key, msg_id=str(uuid4()), cycle_id=ctx["cycle"],
            offer_id=ctx["offer"], counterpart_city_id="COR",
            direction="INCOMING", quantity=50, status="PENDING",
            created_at=ctx["now"],
        ))
        session.commit()
        from app.services.seller_bid_acceptance import reserve_incoming_bid
        result = reserve_incoming_bid(session, bid_idpk=key)
        assert result["status"] == "RESERVED"
        session.rollback()
    state = _state(ctx)
    assert not state["reserves"] and not state["swaps"] and not state["responses"]
    assert state["bids"][0]["status"] == "PENDING"
    _evidence("rollback_atomico", ctx, state)


def test_sesiones_solapadas_lock_held_120ms_segundo_bid_40ms() -> None:
    """Fuerza superposición real de transacciones y contención de MarketOffer."""
    ctx = _context()
    keys = [str(uuid4()), str(uuid4())]
    with Session(engine) as session:
        for key, buyer in zip(keys, ("COR", "HGW")):
            session.add(MarketBid(
                idpk=key, msg_id=str(uuid4()), cycle_id=ctx["cycle"],
                offer_id=ctx["offer"], counterpart_city_id=buyer,
                direction="INCOMING", quantity=80, status="PENDING",
                created_at=ctx["now"],
            ))
        session.commit()

    from app.services.seller_bid_acceptance import reserve_incoming_bid
    barrier = Barrier(2)

    def reserve(index: int) -> dict:
        barrier.wait(timeout=20)
        sleep(index * .040)
        started = perf_counter()
        with Session(engine) as session:
            outcome = reserve_incoming_bid(session, bid_idpk=keys[index])
            if index == 0:
                sleep(.120)  # Mantiene FOR UPDATE bajo transacción sin commit.
            session.commit()
        return {"outcome": outcome, "duration_ms": round((perf_counter() - started) * 1000, 2)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(reserve, 0)
        b = pool.submit(reserve, 1)
        results = [a.result(timeout=40), b.result(timeout=40)]

    assert sorted(r["outcome"]["status"] for r in results) == ["REJECTED", "RESERVED"]
    assert next(r for r in results if r["outcome"]["status"] == "REJECTED")["outcome"]["available"] == 20
    state = _state(ctx)
    _assert_ad4(ctx, state, expected_rejected_available=20)
    _evidence("transacciones_superpuestas_40ms_120ms_lock", ctx, state,
              other={"duraciones_ms": [r["duration_ms"] for r in results],
                     "lock_retained_ms": 120, "second_nominal_delay_ms": 40})


def test_ofertas_diferentes_reservan_sin_contaminar_disponibilidad() -> None:
    """Dos ofertas de un mismo ciclo usan disponibilidad y holds independientes."""
    a_ctx = _context()
    b_ctx = dict(a_ctx, offer=a_ctx["offer"] + "-other", capacity=100)
    with Session(engine) as session:
        session.add(MarketOffer(
            offer_id=b_ctx["offer"], cycle_id=b_ctx["cycle"],
            city_id=CITY_ID, is_local=True,
            initial_quantity=100, remaining_quantity=100,
            price_per_energy=Decimal("7.25"), status="ACTIVE",
            published_at=a_ctx["now"], last_updated_at=a_ctx["now"],
        ))
        session.commit()

    attempts = _burst([_bid(a_ctx, buyer="COR", amount=80),
                       _bid(b_ctx, buyer="HGW", amount=80)], gap_seconds=0)
    a_state, b_state = _state(a_ctx), _state(b_ctx)
    _assert_ad4(a_ctx, a_state)
    _assert_ad4(b_ctx, b_state)
    assert len(a_state["reserves"]) == len(b_state["reserves"]) == 1
    assert a_state["reserves"][0]["quantity"] == 80
    assert b_state["reserves"][0]["quantity"] == 80
    _evidence("dos_ofertas_independientes_A", a_ctx, a_state, attempts=attempts)
    _evidence("dos_ofertas_independientes_B", b_ctx, b_state, attempts=attempts)
