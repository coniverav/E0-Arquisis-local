from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from sqlmodel import Session, select

from ..models import Cycle, LedgerEntry


TWO_DECIMALS = Decimal("0.01")
GIVE_PREMIUM = Decimal("1.05")


class NegotiationRuleViolation(ValueError):
    def __init__(
        self,
        reason: str,
        code: int,
        *,
        cap: Decimal | None = None,
        spare: Decimal | None = None,
    ):
        super().__init__(reason)

        self.reason = reason
        self.code = code
        self.cap = cap
        self.spare = spare


@dataclass(frozen=True)
class NegotiationOfferEvaluation:
    price_cap: Decimal
    settlement_price: Decimal
    spare: Decimal | None


def round2(value: Decimal) -> Decimal:
    return value.quantize(
        TWO_DECIMALS,
        rounding=ROUND_HALF_UP,
    )


def _require_status_values(
    cycle: Cycle,
) -> None:
    if (
        cycle.generation_capacity is None
        or cycle.consumption is None
        or cycle.generation_cost is None
    ):
        raise ValueError(
            f"Cycle {cycle.cycle_id} has no complete status-statement"
        )


def offer_price_cap(
    cycle: Cycle,
) -> Decimal:
    _require_status_values(cycle)

    return round2(
        cycle.generation_cost * GIVE_PREMIUM
    )


def settlement_price(
    cycle: Cycle,
    direction: str,
) -> Decimal:
    _require_status_values(cycle)

    if direction == "take":
        return round2(
            cycle.generation_cost
        )

    if direction == "give":
        return offer_price_cap(cycle)

    raise ValueError(
        f"unsupported negotiation direction: {direction}"
    )


def maximum_sellable_energy(
    cycle: Cycle,
) -> Decimal:
    _require_status_values(cycle)

    available = (
        cycle.generation_capacity
        - cycle.consumption
    )

    return max(
        Decimal("0.00"),
        available,
    )


def sold_energy(
    session: Session,
    cycle_id: str,
) -> Decimal:
    entries = session.exec(
        select(LedgerEntry).where(
            LedgerEntry.cycle_id == cycle_id,
            LedgerEntry.operation_type == "GIVE_CONFIRMED",
        )
    ).all()

    total = Decimal("0.00")

    for entry in entries:
        if entry.energy_delta < 0:
            total += -entry.energy_delta

    return total


def remaining_sellable_energy(
    session: Session,
    cycle: Cycle,
) -> Decimal:
    remaining = (
        maximum_sellable_energy(cycle)
        - sold_energy(
            session,
            cycle.cycle_id,
        )
    )

    return max(
        Decimal("0.00"),
        remaining,
    )


def evaluate_negotiation_offer(
    session: Session,
    *,
    cycle: Cycle,
    direction: str,
    quantity: Decimal,
    price_per_energy: Decimal,
) -> NegotiationOfferEvaluation:
    if direction not in {
        "give",
        "take",
    }:
        raise ValueError(
            f"unsupported negotiation direction: {direction}"
        )

    cap = offer_price_cap(cycle)

    if price_per_energy > cap:
        raise NegotiationRuleViolation(
            "PRICE_ABOVE_CAP",
            422,
            cap=cap,
        )

    spare = None

    if direction == "give":
        spare = remaining_sellable_energy(
            session,
            cycle,
        )

        if quantity > spare:
            raise NegotiationRuleViolation(
                "OVER_CAPACITY",
                409,
                spare=spare,
            )

    return NegotiationOfferEvaluation(
        price_cap=cap,
        settlement_price=settlement_price(
            cycle,
            direction,
        ),
        spare=spare,
    )
