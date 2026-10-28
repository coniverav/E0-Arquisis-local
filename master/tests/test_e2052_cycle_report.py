"""E2-052: el reporte sale dentro de ventana y después del cierre local."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from sqlmodel import Session, select

from app.database import engine
from app.models import Cycle, LedgerEntry, NegotiationReport
from app.services.cycle_accounting import finalize_closed_markets
from app.services.cycle_scheduler import process_cycle_windows
from app.services.negotiation_report_dispatch import enqueue_due_negotiation_reports


def _cycle(session, *, cycle_id, now, opens_at, valid_until, market_closes_at):
    session.add(Cycle(
        cycle_id=cycle_id,
        status_idpk=str(uuid4()),
        status_payload={"market": {}},
        opening_budget_balance=Decimal("0"),
        opening_energy_balance=Decimal("-1000"),
        budget_balance=Decimal("0"),
        energy_balance=Decimal("-1000"),
        last_sequence=0,
        created_at=now,
        market_closes_at=market_closes_at,
        report_window_opens_at=opens_at,
        valid_until=valid_until,
    ))
    session.commit()


def test_report_after_close_reflects_zero_kwh_and_never_leaves_window():
    now = datetime.now(timezone.utc)
    inside = f"e2052-inside-{uuid4()}"
    early = f"e2052-early-{uuid4()}"
    late = f"e2052-late-{uuid4()}"

    with Session(engine) as session:
        _cycle(
            session, cycle_id=inside, now=now,
            opens_at=now - timedelta(seconds=1),
            valid_until=now + timedelta(minutes=5),
            market_closes_at=now - timedelta(seconds=2),
        )
        _cycle(
            session, cycle_id=early, now=now,
            opens_at=now + timedelta(minutes=1),
            valid_until=now + timedelta(minutes=5),
            market_closes_at=now + timedelta(minutes=1),
        )
        _cycle(
            session, cycle_id=late, now=now,
            opens_at=now - timedelta(minutes=6),
            valid_until=now - timedelta(seconds=1),
            market_closes_at=now - timedelta(minutes=6),
        )

        windows = process_cycle_windows(session, now=now)
        assert windows["report_windows_opened"] == 1
        assert windows["cycles_closed"] == 1
        market = finalize_closed_markets(session, now=now)
        # Ambos ciclos cerrados llegan con KWH local distinto de 0.
        assert market["burns_applied"] == 2

        assert enqueue_due_negotiation_reports(
            session, city_id="KLD", routing_key="central", now=now,
        ) == 1
        session.commit()

        report = session.exec(
            select(NegotiationReport).where(NegotiationReport.cycle_id == inside)
        ).one()
        assert report.payload["data"] == {"budgetBalance": 0, "energyBalance": 0}
        assert session.exec(
            select(NegotiationReport).where(NegotiationReport.cycle_id == early)
        ).all() == []
        assert session.exec(
            select(NegotiationReport).where(NegotiationReport.cycle_id == late)
        ).all() == []

        # Un segundo tick no duplica el reporte ni vuelve a quemar energía.
        assert enqueue_due_negotiation_reports(
            session, city_id="KLD", routing_key="central", now=now,
        ) == 0
        assert finalize_closed_markets(session, now=now)["burns_applied"] == 0
        for cycle_id in (inside, late):
            cycle = session.get(Cycle, cycle_id)
            assert cycle.energy_balance == 0
            entries = session.exec(
                select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
            ).all()
            assert len(entries) == 1
            assert entries[0].operation_type == "IMBALANCE_BURN_FALLBACK"

        # El ciclo cuya ventana aún no abre conserva su energía.
        assert session.get(Cycle, early).energy_balance == Decimal("-1000.00")
        assert session.exec(
            select(LedgerEntry).where(LedgerEntry.cycle_id == early)
        ).all() == []
