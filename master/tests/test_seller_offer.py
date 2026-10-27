
from decimal import Decimal

from app.services.seller_offer import calculate_offer


def test_uses_real_wallet_available():
    quantity, price = calculate_offer(
        sellable=20000,
        wallet_available=Decimal("15400"),
        generation_cost=Decimal("210"),
        buyback_price=Decimal("220.50"),
    )

    assert quantity == 15400
    assert price == Decimal("252.00")


def test_fraction_and_minimum_price():
    quantity, price = calculate_offer(
        sellable=1001,
        wallet_available=Decimal("2000"),
        generation_cost=Decimal("210"),
        buyback_price=Decimal("220.50"),
        markup=Decimal("1.02"),
        fraction=Decimal("0.50"),
    )

    assert quantity == 500
    assert price == Decimal("220.50")


def test_no_sellable_energy():
    quantity, _ = calculate_offer(
        sellable=0,
        wallet_available=Decimal("2000"),
        generation_cost=Decimal("210"),
        buyback_price=Decimal("220.50"),
    )

    assert quantity == 0
