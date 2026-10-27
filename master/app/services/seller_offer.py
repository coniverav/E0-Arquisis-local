
"""Creación automática de ofertas de venta."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from uuid import uuid4

from sqlmodel import Session, select

from app.config import CITY_ID
from app.database import engine
from app.models import Cycle, MarketOffer, OutboundMessage, StrategyConfig
from app.services.wallet_client import WalletClient


CENT = Decimal("0.01")
DEFAULT_MARKUP = Decimal("1.20")
DEFAULT_FRACTION = Decimal("1.00")


def calculate_offer(
    *,
    sellable: int,
    wallet_available: Decimal,
    generation_cost: Decimal,
    buyback_price: Decimal,
    markup: Decimal = DEFAULT_MARKUP,
    fraction: Decimal = DEFAULT_FRACTION,
) -> tuple[int, Decimal]:
    """Calcula cantidad y precio sin ofrecer más KWH que los disponibles."""

    if not Decimal("0") <= fraction <= Decimal("1"):
        raise ValueError("offer_quantity_fraction debe estar entre 0 y 1")
    if markup <= 0:
        raise ValueError("offer_price_markup debe ser positivo")

    wallet_kwh = max(
        0, int(wallet_available.to_integral_value(rounding=ROUND_DOWN))
    )
    available = min(max(0, sellable), wallet_kwh)

    quantity = int(
        (Decimal(available) * fraction).to_integral_value(rounding=ROUND_DOWN)
    )

    price = max(
        buyback_price,
        (generation_cost * markup).quantize(CENT, rounding=ROUND_HALF_UP),
    )

    return quantity, price.quantize(CENT, rounding=ROUND_HALF_UP)


def _eligible(cycle: Cycle | None, now: datetime) -> bool:
    """Comprueba si corresponde abrir una oferta para el ciclo."""

    return bool(
        cycle is not None
        and cycle.status_idpk is not None
        and cycle.market_opens_at is not None
        and cycle.market_last_offer_at is not None
        and cycle.market_opens_at <= now
        and now + timedelta(seconds=5) < cycle.market_last_offer_at
        and int(
            (cycle.status_payload or {}).get("energy", {}).get("sellable", 0)
        ) > 0
    )


async def enqueue_seller_offer(
    cycle_id: str,
    *,
    wallet_client: WalletClient | None = None,
) -> bool:
    """Registra una única oferta local y su publicación en el outbox."""

    now = datetime.now(timezone.utc)

    # Identificador estable para la misma ciudad y ciclo.
    offer_id = f"{CITY_ID}-{cycle_id}"

    # Primera validación, antes de consultar la wallet.
    with Session(engine) as session:
        cycle = session.get(Cycle, cycle_id)

        if not _eligible(cycle, now):
            return False

        existing = session.exec(
            select(MarketOffer).where(MarketOffer.offer_id == offer_id)
        ).first()

        if existing:
            return False

    # La wallet es la fuente oficial de energía disponible.
    # Esta llamada ocurre sin mantener bloqueos sobre la BD.
    balance = await (wallet_client or WalletClient()).get_balance()

    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        with session.begin():

            # Bloqueo para impedir dos ofertas simultáneas del mismo ciclo.
            cycle = session.exec(
                select(Cycle)
                .where(Cycle.cycle_id == cycle_id)
                .with_for_update()
            ).first()

            if not _eligible(cycle, now):
                return False

            existing = session.exec(
                select(MarketOffer).where(MarketOffer.offer_id == offer_id)
            ).first()

            if existing:
                return False

            status = cycle.status_payload or {}
            energy = status.get("energy", {})
            market = status.get("market", {})

            if "buybackPrice" not in market:
                raise ValueError(
                    "status-statement no trae market.buybackPrice"
                )

            # Utilizar estrategia activa o valores predeterminados.
            strategy = session.exec(
                select(StrategyConfig)
                .where(StrategyConfig.status == "ACTIVE")
                .order_by(StrategyConfig.version.desc())
            ).first()

            quantity, price = calculate_offer(
                sellable=int(energy["sellable"]),
                wallet_available=balance.kwh_available,
                generation_cost=Decimal(str(energy["generationCost"])),
                buyback_price=Decimal(str(market["buybackPrice"])),
                markup=(
                    strategy.offer_price_markup
                    if strategy else DEFAULT_MARKUP
                ),
                fraction=(
                    strategy.offer_quantity_fraction
                    if strategy else DEFAULT_FRACTION
                ),
            )

            if quantity <= 0:
                return False

            # Identificadores independientes para el mensaje.
            msg_id = str(uuid4())
            idpk = str(uuid4())

            payload = {
                "msgId": msg_id,
                "idpk": idpk,
                "type": "offer",
                "timestamp": now.isoformat().replace("+00:00", "Z"),
                "cycleId": cycle_id,
                "cityId": CITY_ID,
                "data": {
                    "offerId": offer_id,
                    "quantity": quantity,
                    "pricePerEnergy": float(price),
                },
            }

            # Guardar la oferta local.
            session.add(
                MarketOffer(
                    offer_id=offer_id,
                    cycle_id=cycle_id,
                    city_id=CITY_ID,
                    is_local=True,
                    initial_quantity=quantity,
                    remaining_quantity=quantity,
                    price_per_energy=price,
                    status="ACTIVE",
                    published_at=now,
                    last_updated_at=now,
                )
            )

            # Guardar el mensaje para publicación AMQP.
            session.add(
                OutboundMessage(
                    msg_id=msg_id,
                    idpk=idpk,
                    message_type="offer",
                    cycle_id=cycle_id,
                    payload=payload,
                    routing_key=f"market.offer.{CITY_ID}",
                    exchange_name="market",
                    status="PENDING",
                    attempt_count=0,
                    dispatch_required=True,
                    available_at=cycle.market_opens_at,
                    expires_at=cycle.market_last_offer_at,
                    created_at=now,
                )
            )

    # La transacción confirmó oferta + mensaje en conjunto.
    return True
