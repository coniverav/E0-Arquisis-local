"""E2-044: conciliación del ledger local con `GET /balance` y `/statement`.

La wallet es la autoridad económica. Este módulo:

1. Consulta el saldo oficial y guarda un snapshot `REST_SYNC`.
2. Recorre todas las páginas de `GET /statement` (`entries` + `nextCursor`)
   y persiste los asientos con deduplicación por `entryId`.
3. Compara el último snapshot `EVENT` (derivado de `swap-settled`) con el
   saldo oficial y explica la diferencia con los asientos posteriores y los
   hechos `trade-recorded` / `imbalance-settlement`.

El resultado se guarda con `authority="wallet"`: un snapshot local nunca se
declara autoridad ni corrige a la wallet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import logging
import time
from typing import Any, Optional

from sqlmodel import Session, select
from sqlalchemy import func

from app.config import CITY_ID, WALLET_RECONCILE_INTERVAL_SECONDS
from app.database import engine
from app.models import (
    ImbalanceSettlement,
    Cycle,
    LedgerEntry,
    SwapRecord,
    TradeRecord,
    WalletBalanceSnapshot,
    WalletReconciliation,
    WalletStatementEntry,
)
from app.services.wallet_client import WalletClient, WalletError, WalletStatementPage

logger = logging.getLogger(__name__)

_last_tick_monotonic: float | None = None


def _parse_instant(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WalletError("asiento de statement con monto no numérico") from exc
    if not result.is_finite():
        raise WalletError("asiento de statement con monto no finito")
    return result


async def fetch_statement_entries(
    client: WalletClient,
    *,
    cycle_id: Optional[str] = None,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    """Recorre `GET /statement` hasta `nextCursor = null`."""

    entries: list[dict[str, Any]] = []
    cursor: Optional[str] = None
    seen_cursors: set[str] = set()

    for _ in range(max_pages):
        page: WalletStatementPage = await client.get_statement(
            cycle_id=cycle_id, cursor=cursor,
        )
        entries.extend(page.entries)
        cursor = page.next_cursor
        if cursor is None:
            return entries
        if cursor in seen_cursors:
            raise WalletError("GET /statement repitió el mismo nextCursor")
        seen_cursors.add(cursor)

    raise WalletError("GET /statement excedió el máximo de páginas")


async def fetch_incremental_statement_entries(
    client: WalletClient,
    session: Session,
    *,
    full_scan: bool,
    max_pages: int = 100,
    overlap_pages: int = 2,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Lee desde la cabeza; nunca guarda un cursor opaco entre ejecuciones.

    Una revisión completa periódica recupera asientos insertados detrás del
    solapamiento. Si excede el presupuesto, falla sin declarar un MATCH.
    """
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    known_pages = 0
    for page_number in range(1, max_pages + 1):
        page = await client.get_statement(cursor=cursor)
        entries.extend(page.entries)
        ids = [item.get("entryId") for item in page.entries if isinstance(item, dict)]
        known = set(session.exec(select(WalletStatementEntry.entry_id).where(
            WalletStatementEntry.entry_id.in_(ids)
        )).all()) if ids else set()
        known_pages = known_pages + 1 if ids and len(known) == len(ids) else 0
        cursor = page.next_cursor
        if cursor is None:
            return entries, True, page_number
        if cursor in seen_cursors:
            raise WalletError("GET /statement repitió el mismo nextCursor")
        seen_cursors.add(cursor)
        if not full_scan and known_pages >= overlap_pages:
            return entries, False, page_number
    raise WalletError("GET /statement excedió el máximo de páginas")


def store_statement_entries(
    session: Session,
    entries: list[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> int:
    """Persiste asientos nuevos; devuelve cuántos no existían."""

    fetched_at = now or datetime.now(timezone.utc)
    created = 0

    for entry in entries:
        entry_id = entry.get("entryId")
        if not isinstance(entry_id, str) or not entry_id:
            raise WalletError("statement entry sin entryId")

        existing = session.exec(
            select(WalletStatementEntry).where(
                WalletStatementEntry.entry_id == entry_id
            )
        ).first()
        if existing is not None:
            continue

        asset = entry.get("asset")
        kind = entry.get("kind")
        created_at = _parse_instant(entry.get("createdAt"))
        if asset not in {"CR", "KWH"} or not isinstance(kind, str) or created_at is None:
            raise WalletError(f"statement entry {entry_id} inválido")
        reference = entry.get("reference")
        if not isinstance(reference, str) or not reference:
            raise WalletError(f"statement entry {entry_id} sin reference")

        session.add(WalletStatementEntry(
            entry_id=entry_id,
            cycle_id=entry.get("cycleId"),
            asset=asset,
            kind=kind,
            amount=_decimal(entry.get("amount")),
            counterpart=entry.get("counterparty"),
            reference=reference,
            created_at=created_at,
            fetched_at=fetched_at,
        ))
        created += 1

    session.flush()
    return created


def _latest_event_snapshot(
    session: Session,
    *,
    before: Optional[datetime] = None,
) -> Optional[WalletBalanceSnapshot]:
    query = select(WalletBalanceSnapshot).where(
        WalletBalanceSnapshot.source == "EVENT"
    )
    event_time = func.coalesce(
        WalletBalanceSnapshot.wallet_as_of, WalletBalanceSnapshot.recorded_at
    )
    if before is not None:
        query = query.where(event_time <= before)
    return session.exec(
        query.order_by(event_time.desc(), WalletBalanceSnapshot.id.desc())
    ).first()


def _sum_statement(
    session: Session,
    *,
    after: datetime,
    before: datetime | None = None,
    cycle_id: Optional[str] = None,
) -> tuple[dict[str, Decimal], dict[str, dict[str, Decimal]]]:
    query = select(WalletStatementEntry).where(
        WalletStatementEntry.created_at > after
    )
    if before is not None:
        query = query.where(WalletStatementEntry.created_at <= before)
    if cycle_id is not None:
        query = query.where(WalletStatementEntry.cycle_id == cycle_id)

    totals = {"CR": Decimal("0"), "KWH": Decimal("0")}
    by_kind: dict[str, dict[str, Decimal]] = {"CR": {}, "KWH": {}}
    for entry in session.exec(query).all():
        totals[entry.asset] += entry.amount
        bucket = by_kind[entry.asset]
        bucket[entry.kind] = bucket.get(entry.kind, Decimal("0")) + entry.amount
    return totals, by_kind


def _local_fact_counts(
    session: Session,
    *,
    after: datetime,
    cycle_id: Optional[str],
) -> dict[str, int]:
    trade_query = select(TradeRecord).where(TradeRecord.recorded_at > after)
    imbalance_query = select(ImbalanceSettlement).where(
        ImbalanceSettlement.recorded_at > after
    )
    if cycle_id is not None:
        trade_query = trade_query.where(TradeRecord.cycle_id == cycle_id)
        imbalance_query = imbalance_query.where(
            ImbalanceSettlement.cycle_id == cycle_id
        )
    return {
        "trade_records": len(session.exec(trade_query).all()),
        "imbalance_settlements": len(session.exec(imbalance_query).all()),
    }


def _operation_evidence(
    session: Session, *, cycle_id: str | None, wallet_as_of: datetime,
    balance: Any,
) -> tuple[dict[str, Any], bool, bool]:
    """Contrasta hechos del ciclo por activo y origen, sin inferir referencias.

    Devuelve evidencia, discrepancia confirmada y operaciones pendientes.
    Una referencia opaca que no coincide no se convierte en un swap inventado.
    """
    if cycle_id is None:
        return {"cycle_id": None, "cycle_present": False, "operations": []}, False, False
    cycle = session.get(Cycle, cycle_id)
    statement = session.exec(select(WalletStatementEntry).where(
        WalletStatementEntry.cycle_id == cycle_id,
        WalletStatementEntry.created_at <= wallet_as_of,
    ).order_by(WalletStatementEntry.created_at, WalletStatementEntry.entry_id)).all()
    ledger = session.exec(select(LedgerEntry).where(
        LedgerEntry.cycle_id == cycle_id,
    ).order_by(LedgerEntry.sequence_no)).all()

    # La wallet divide funding bruto y penalty; el transfer local trae el neto.
    # El settlement puede producir buyback e imbalance como asientos separados.
    kinds = {
        "swap": "SWAP_SETTLED", "transport": "SWAP_TRANSPORT",
        "funding": "TRANSFER_IN", "penalty": "TRANSFER_IN",
        "buyback": "IMBALANCE_SETTLEMENT", "imbalance": "IMBALANCE_SETTLEMENT",
        "burn": "IMBALANCE_BURN", "mint": "OPENING_MINT",
    }
    amounts: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    entry_ids: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    kind_names: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for entry in statement:
        operation = kinds.get(entry.kind, "WALLET_ONLY")
        reference = entry.reference if operation in {"SWAP_SETTLED", "SWAP_TRANSPORT", "WALLET_ONLY"} else cycle_id
        key = (operation, reference, entry.asset)
        amounts[key] += entry.amount
        entry_ids[key].append(entry.entry_id)
        kind_names[key].add(entry.kind)

    local_amounts: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    local_ids: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for entry in ledger:
        if entry.operation_type not in {"SWAP_SETTLED", "SWAP_TRANSPORT", "TRANSFER_IN", "IMBALANCE_SETTLEMENT", "IMBALANCE_BURN"}:
            continue
        reference = entry.idpk if entry.operation_type in {"SWAP_SETTLED", "SWAP_TRANSPORT"} else cycle_id
        for asset, amount in (("CR", entry.budget_delta), ("KWH", entry.energy_delta)):
            if amount == 0:
                continue
            key = (entry.operation_type, reference, asset)
            local_amounts[key] += amount
            local_ids[key].append(entry.id)
    if cycle is not None and isinstance(cycle.status_payload, dict):
        energy = cycle.status_payload.get("energy")
        sellable = energy.get("sellable") if isinstance(energy, dict) else None
        if isinstance(sellable, (int, float)) and not isinstance(sellable, bool) and sellable > 0:
            local_amounts[("OPENING_MINT", cycle_id, "KWH")] = Decimal(str(sellable))

    operations: list[dict[str, Any]] = []
    mismatch = False
    pending = False
    for key in sorted(set(amounts) | set(local_amounts)):
        operation, reference, asset = key
        wallet_amount = amounts.get(key)
        local_amount = local_amounts.get(key)
        if operation == "WALLET_ONLY":
            state = "WALLET_ONLY"
            pending = True
        elif wallet_amount is None or local_amount is None:
            state = "PENDING_WALLET" if wallet_amount is None else "PENDING_LOCAL"
            pending = True
        elif wallet_amount != local_amount:
            state = "MISMATCH"
            mismatch = True
        else:
            state = "MATCH"
        swap = session.exec(select(SwapRecord).where(
            SwapRecord.swap_id == reference
        )).first() if operation in {"SWAP_SETTLED", "SWAP_TRANSPORT"} else None
        operations.append({
            "cycle_id": cycle_id, "operation": operation,
            "statement_kinds": sorted(kind_names.get(key, set())),
            "reference": reference, "asset": asset,
            "wallet_entry_ids": entry_ids.get(key, []),
            "ledger_entry_ids": local_ids.get(key, []),
            "swap_id": swap.swap_id if swap else None,
            "bid_idpk": swap.source_bid_idpk if swap else None,
            "wallet_amount": str(wallet_amount) if wallet_amount is not None else None,
            "local_amount": str(local_amount) if local_amount is not None else None,
            "difference": str(wallet_amount - local_amount) if wallet_amount is not None and local_amount is not None else None,
            "status": state,
        })

    result: dict[str, Any] = {
        "cycle_id": cycle_id, "cycle_present": cycle is not None,
        "operations": operations,
    }
    if cycle is not None:
        rebuilt_cr = cycle.opening_budget_balance + sum((entry.budget_delta for entry in ledger), Decimal("0"))
        rebuilt_kwh = cycle.opening_energy_balance + sum((entry.energy_delta for entry in ledger), Decimal("0"))
        consistent = rebuilt_cr == cycle.budget_balance and rebuilt_kwh == cycle.energy_balance
        result["ledger_materialization"] = {
            "budget_rebuilt": str(rebuilt_cr), "budget_cycle": str(cycle.budget_balance),
            "energy_rebuilt": str(rebuilt_kwh), "energy_cycle": str(cycle.energy_balance),
            "consistent": consistent,
        }
        mismatch |= not consistent
        if balance.last_settled_cycle == cycle_id and balance.balance_at_last_close_cr is not None:
            difference = Decimal(balance.balance_at_last_close_cr) - cycle.budget_balance
            result["last_close"] = {
                "wallet_cr": str(balance.balance_at_last_close_cr),
                "cycle_cr": str(cycle.budget_balance), "difference": str(difference),
            }
            mismatch |= difference != 0
        elif session.exec(select(func.count(Cycle.cycle_id)).where(
            Cycle.status_idpk.is_not(None)
        )).one() == 1:
            # Solo con un ciclo conocido se puede atribuir el saldo global
            # actual a su proyección local sin mezclar ciclos superpuestos.
            wallet_total = balance.cr_available + balance.cr_held
            difference = wallet_total - cycle.budget_balance
            result["single_cycle_cr"] = {
                "wallet_available_plus_held": str(wallet_total),
                "cycle_budget": str(cycle.budget_balance),
                "difference": str(difference),
                "ledger_entry_ids": [entry.id for entry in ledger if entry.budget_delta],
            }
            mismatch |= difference != 0
    return result, mismatch, pending


async def reconcile_wallet(
    session: Session,
    *,
    client: Optional[WalletClient] = None,
    cycle_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> WalletReconciliation:
    """Compara ledger local y wallet, persiste snapshot, asientos y reporte."""

    current = now or datetime.now(timezone.utc)
    wallet_client = client or WalletClient()

    balance = await wallet_client.get_balance()
    official_as_of = _parse_instant(balance.timestamp)
    wallet_as_of = official_as_of or current

    # 1. Snapshot oficial (REST_SYNC). La wallet manda.
    snapshot = WalletBalanceSnapshot(
        cycle_id=cycle_id,
        source="REST_SYNC",
        wallet_as_of=official_as_of,
        kwh_available=balance.kwh_available,
        kwh_held=balance.kwh_held,
        cr_available=balance.cr_available,
        cr_held=balance.cr_held,
        credit_line=balance.credit_line,
        recorded_at=current,
    )
    session.add(snapshot)

    # 2. Un barrido completo inicial y cada diez reconciliaciones recupera
    # inserciones antiguas; los demás leen la cabeza con solapamiento.
    prior_reports = session.exec(select(func.count(WalletReconciliation.id))).one()
    full_scan = prior_reports % 10 == 0
    entries, statement_complete, pages = await fetch_incremental_statement_entries(
        wallet_client, session, full_scan=full_scan,
    )
    new_entries = store_statement_entries(session, entries, now=current)

    # 3. Comparación contra el último saldo derivado de eventos.
    baseline = _latest_event_snapshot(session, before=wallet_as_of)
    target_cycle = cycle_id or balance.last_settled_cycle or (baseline.cycle_id if baseline else None)
    local_evidence, local_mismatch, local_pending = _operation_evidence(
        session, cycle_id=target_cycle, wallet_as_of=wallet_as_of, balance=balance,
    )

    status = "NO_LOCAL_BASELINE"
    cr_local = kwh_local = None
    cr_difference = kwh_difference = None
    cr_explained = kwh_explained = None
    cr_unexplained = kwh_unexplained = None
    explanation: dict[str, Any] = {
        "city_id": CITY_ID,
        "authority": "wallet",
        "wallet_as_of": wallet_as_of.isoformat(),
        "statement_entries_total": len(entries),
        "statement_entries_new": new_entries,
        "statement_pages": pages,
        "statement_full_scan": full_scan,
        "statement_complete": statement_complete,
        "cycle_id": target_cycle,
        "local_evidence": local_evidence,
        "official_as_of_valid": official_as_of is not None,
    }

    if baseline is not None:
        cr_local = baseline.cr_available
        kwh_local = baseline.kwh_available

        baseline_as_of = baseline.wallet_as_of or baseline.recorded_at
        explained, by_kind = _sum_statement(
            session, after=baseline_as_of, before=wallet_as_of, cycle_id=None,
        )
        cr_explained = explained["CR"]
        kwh_explained = explained["KWH"]

        cr_difference = balance.cr_available - cr_local
        kwh_difference = balance.kwh_available - kwh_local

        cr_unexplained = cr_difference - cr_explained
        kwh_unexplained = kwh_difference - kwh_explained

        if cr_unexplained == 0 and kwh_unexplained == 0:
            status = (
                "MATCH"
                if cr_explained == 0 and kwh_explained == 0
                else "EXPLAINED"
            )
        else:
            status = "DIVERGENT"

        explanation.update({
            "baseline_snapshot_id": baseline.id,
            "baseline_recorded_at": baseline.recorded_at.isoformat(),
            "baseline_wallet_as_of": baseline.wallet_as_of.isoformat() if baseline.wallet_as_of else None,
            "baseline_source_swap_id": baseline.source_swap_id,
            "baseline_time_uncertain": baseline.wallet_as_of is None,
            "statement_by_kind": {
                asset: {kind: str(value) for kind, value in values.items()}
                for asset, values in by_kind.items()
            },
            "local_facts": _local_fact_counts(
                session, after=baseline.recorded_at, cycle_id=cycle_id,
            ),
        })

        held_difference = {
            "CR": balance.cr_held - baseline.cr_held,
            "KWH": balance.kwh_held - baseline.kwh_held,
        }
        pending_swaps = session.exec(select(SwapRecord).where(
            SwapRecord.status.in_(["SUBMITTED", "PROPOSED", "ACCEPT_SUBMITTED"])
        )).all()
        explanation["held"] = {
            "difference": {asset: str(value) for asset, value in held_difference.items()},
            "pending_swap_ids": [row.swap_id for row in pending_swaps if row.swap_id],
            "pending_swap_record_ids": [row.id for row in pending_swaps],
        }
        if any(held_difference.values()):
            if pending_swaps and status == "MATCH":
                status = "EXPLAINED"
            elif not pending_swaps:
                status = "DIVERGENT"

    if local_mismatch and (baseline is not None or "last_close" in local_evidence):
        status = "DIVERGENT"
    elif local_mismatch:
        explanation["local_difference_without_global_baseline"] = True
    elif (local_pending or not statement_complete or official_as_of is None
          or (baseline is not None and baseline.wallet_as_of is None)) and status == "MATCH":
        # Una coincidencia entre snapshots no demuestra acuerdo del ledger.
        status = "EXPLAINED"

    if not local_evidence["cycle_present"] and status == "MATCH":
        # Un EVENT es evidencia de Wallet, no reemplaza al ciclo ni al ledger.
        # Un swap-settled puede persistir snapshot aunque falte su Cycle local.
        status = "NO_LOCAL_BASELINE"
        cr_local = kwh_local = None
        cr_difference = kwh_difference = None
        cr_explained = kwh_explained = None
        cr_unexplained = kwh_unexplained = None

    report = WalletReconciliation(
        cycle_id=target_cycle,
        status=status,
        authority="wallet",
        cr_available_local=cr_local,
        cr_available_wallet=balance.cr_available,
        cr_difference=cr_difference,
        cr_explained=cr_explained,
        cr_unexplained=cr_unexplained,
        kwh_available_local=kwh_local,
        kwh_available_wallet=balance.kwh_available,
        kwh_difference=kwh_difference,
        kwh_explained=kwh_explained,
        kwh_unexplained=kwh_unexplained,
        explanation=explanation,
        created_at=current,
    )
    session.add(report)
    session.commit()
    session.refresh(report)

    if status == "DIVERGENT":
        logger.error(
            "Conciliación wallet divergente: CR diff=%s no explicado=%s; "
            "KWH diff=%s no explicado=%s",
            cr_difference, cr_unexplained, kwh_difference, kwh_unexplained,
        )
    else:
        logger.info("Conciliación wallet: %s", status)

    return report


async def reconcile_wallet_tick(
    *,
    interval_seconds: Optional[float] = None,
    client: Optional[WalletClient] = None,
    now: Optional[datetime] = None,
) -> Optional[WalletReconciliation]:
    """Ejecuta la conciliación como mucho una vez por intervalo.

    El scheduler puede llamarla en cada vuelta sin golpear la wallet cada
    pocos segundos. Devuelve `None` si todavía no corresponde o si la wallet
    no está disponible (el tick nunca tumba al scheduler).
    """

    global _last_tick_monotonic

    interval = (
        WALLET_RECONCILE_INTERVAL_SECONDS
        if interval_seconds is None
        else interval_seconds
    )
    current_tick = time.monotonic()
    if (_last_tick_monotonic is not None and interval > 0
            and current_tick - _last_tick_monotonic < interval):
        return None

    _last_tick_monotonic = current_tick
    try:
        with Session(engine) as session:
            return await reconcile_wallet(session, client=client, now=now)
    except (WalletError, OSError) as exc:
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, int) and retry_after > interval:
            _last_tick_monotonic = current_tick + retry_after - interval
        logger.warning("Conciliación wallet no disponible: %s", exc)
        return None
    except Exception:
        logger.exception("Error inesperado en la conciliación wallet")
        return None
