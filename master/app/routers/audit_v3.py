"""E2-075: trazas de sólo lectura para RF05 y la demo v3.

Se construyen respuestas en lista blanca. Ni payloads, JWT de swap,
Idempotency-Key ni excepciones de HTTP atraviesan estos DTO.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, false, func, or_
from sqlmodel import Session, select

from ..auth import verify_jwt
from ..database import get_session
from ..models import (
    InboundMessage, LedgerEntry, OutboundMessage, SwapRecord,
    WalletBalanceSnapshot, WalletReconciliation, WalletStatementEntry,
)

def _operator(claims: dict = Depends(verify_jwt)) -> None:
    # La configuración Auth0 existente valida firma, issuer y audience.
    # Una identidad de usuario es requisito adicional para estas trazas.
    if not isinstance(claims, dict) or not str(claims.get("sub") or "").strip():
        raise HTTPException(status_code=403, detail="User identity required")


router = APIRouter(prefix="/audit", tags=["audit-v3"], dependencies=[Depends(_operator)])
WALLET_TYPES = ("swap-proposed", "swap-settled", "swap-rejected", "swap-expired", "swap-cancelled")
INTERCITY_TYPES = ("bid", "bid-accepted", "bid-rejected", "ack", "nack")
NACK_REASONS = frozenset({
    "MALFORMED_MESSAGE", "UNKNOWN_TYPE", "IDPK_EQUALS_MSGID", "IDENTITY_MISMATCH",
})


class AuditPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[dict[str, Any]]


def _page(total: int, offset: int, limit: int, items: list[dict[str, Any]]) -> AuditPage:
    return AuditPage(total=total, offset=offset, limit=limit, items=items)


def _safe_fields(source: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Evita publicar nuevos campos internos por accidente al extender AD5."""
    return {key: value for key, value in source.items() if key in allowed}


@router.get("/intercity", response_model=AuditPage)
def intercity_audit(
    cycle_id: str | None = Query(None, alias="cycleId"),
    idpk: str | None = None,
    msg_id: str | None = Query(None, alias="msgId"),
    city_id: str | None = Query(None, alias="cityId"),
    status: str | None = None,
    reason_code: str | None = Query(None, alias="reasonCode"),
    direction: Literal["INBOUND", "OUTBOUND"] | None = None,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> AuditPage:
    inbound_conditions = [InboundMessage.message_type.in_(INTERCITY_TYPES)]
    outbound_conditions = [OutboundMessage.message_type.in_(INTERCITY_TYPES)]
    if cycle_id is not None:
        inbound_conditions.append(InboundMessage.cycle_id == cycle_id)
        outbound_conditions.append(OutboundMessage.cycle_id == cycle_id)
    if idpk is not None:
        inbound_conditions.append(InboundMessage.idpk == idpk)
        outbound_conditions.append(OutboundMessage.idpk == idpk)
    if msg_id is not None:
        inbound_conditions.append(or_(InboundMessage.msg_id == msg_id,
                                      InboundMessage.payload["data"]["target"].astext == msg_id))
        outbound_conditions.append(or_(OutboundMessage.msg_id == msg_id,
                                       OutboundMessage.target_msg_id == msg_id))
    if city_id is not None:
        inbound_conditions.append(InboundMessage.sender == city_id)
        outbound_conditions.append(OutboundMessage.routing_key == f"city.{city_id}")
    if status is not None:
        inbound_conditions.append(InboundMessage.status == status)
        outbound_conditions.append(OutboundMessage.status == status)
    if reason_code is not None:
        inbound_conditions.append(InboundMessage.reason_code == reason_code)
        outbound_conditions.append(and_(
            OutboundMessage.message_type == "nack",
            OutboundMessage.payload["reason"].astext == reason_code,
        ) if reason_code in NACK_REASONS else false())

    rows: list[tuple[datetime, int, dict[str, Any]]] = []
    total = 0
    if direction != "OUTBOUND":
        total += session.exec(select(func.count(InboundMessage.id)).where(*inbound_conditions)).one()
        incoming = session.exec(select(InboundMessage).where(*inbound_conditions)
            .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
            .limit(offset + limit)).all()
        for row in incoming:
            data = (row.payload or {}).get("data")
            target = data.get("target") if isinstance(data, dict) else None
            rows.append((row.received_at, row.id, {
                "direction": "INBOUND", "msgId": row.msg_id, "idpk": row.idpk,
                "type": row.message_type, "cycleId": row.cycle_id,
                "cityId": row.sender, "status": row.status,
                "reasonCode": row.reason_code, "targetMsgId": target if isinstance(target, str) else None,
                "occurredAt": row.received_at,
            }))
    if direction != "INBOUND":
        total += session.exec(select(func.count(OutboundMessage.id)).where(*outbound_conditions)).one()
        outgoing = session.exec(select(OutboundMessage).where(*outbound_conditions)
            .order_by(OutboundMessage.created_at.desc(), OutboundMessage.id.desc())
            .limit(offset + limit)).all()
        for row in outgoing:
            payload_reason = row.payload.get("reason") if isinstance(row.payload, dict) else None
            rows.append((row.created_at, row.id, {
                "direction": "OUTBOUND", "msgId": row.msg_id, "idpk": row.idpk,
                "type": row.message_type, "cycleId": row.cycle_id,
                "cityId": row.routing_key[5:] if row.routing_key and row.routing_key.startswith("city.") else None,
                "status": row.status,
                "reasonCode": payload_reason if row.message_type == "nack" and isinstance(payload_reason, str) and payload_reason in NACK_REASONS else None,
                "targetMsgId": row.target_msg_id, "occurredAt": row.created_at,
            }))
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return _page(total, offset, limit, [row[2] for row in rows[offset:offset + limit]])


@router.get("/wallet-events", response_model=AuditPage)
def wallet_event_audit(
    cycle_id: str | None = Query(None, alias="cycleId"),
    idpk: str | None = None,
    swap_id: str | None = Query(None, alias="swapId"),
    status: str | None = None,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> AuditPage:
    swap_expr = InboundMessage.payload["data"]["swapId"].astext
    conditions = [InboundMessage.message_type.in_(WALLET_TYPES)]
    related_swap_ids: list[str] = []
    if cycle_id is not None:
        related_swap_ids = [value for value in session.exec(select(SwapRecord.swap_id).where(
            SwapRecord.cycle_id == cycle_id, SwapRecord.swap_id.is_not(None)
        )).all() if value]
        conditions.append(or_(InboundMessage.cycle_id == cycle_id,
                              swap_expr.in_(related_swap_ids)) if related_swap_ids else InboundMessage.cycle_id == cycle_id)
    if idpk is not None:
        conditions.append(InboundMessage.idpk == idpk)
    if swap_id is not None:
        conditions.append(swap_expr == swap_id)
    if status is not None:
        conditions.append(InboundMessage.status == status)
    total = session.exec(select(func.count(InboundMessage.id)).where(*conditions)).one()
    events = session.exec(select(InboundMessage).where(*conditions)
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
        .offset(offset).limit(limit)).all()
    items = []
    for event in events:
        data = (event.payload or {}).get("data")
        event_swap_id = data.get("swapId") if isinstance(data, dict) else None
        event_swap_id = event_swap_id if isinstance(event_swap_id, str) else None
        swap = session.exec(select(SwapRecord).where(SwapRecord.swap_id == event_swap_id)).first() if event_swap_id else None
        linked_cycle = swap.cycle_id if swap else event.cycle_id
        ledger_ids = session.exec(select(LedgerEntry.id).where(
            LedgerEntry.idpk == event_swap_id,
            LedgerEntry.operation_type.in_(["SWAP_SETTLED", "SWAP_TRANSPORT"]),
        )).all() if event_swap_id else []
        statement_ids = session.exec(select(WalletStatementEntry.entry_id).where(
            WalletStatementEntry.reference == event_swap_id
        )).all() if event_swap_id else []
        snapshot_ids = session.exec(select(WalletBalanceSnapshot.id).where(
            WalletBalanceSnapshot.source_swap_id == event_swap_id
        )).all() if event_swap_id else []
        reconciliation = session.exec(select(WalletReconciliation).where(
            WalletReconciliation.cycle_id == linked_cycle
        ).order_by(WalletReconciliation.created_at.desc()).limit(1)).first() if linked_cycle else None
        items.append({
            "msgId": event.msg_id, "idpk": event.idpk,
            "type": event.message_type, "status": event.status,
            "reasonCode": event.reason_code, "receivedAt": event.received_at,
            "swapId": event_swap_id, "cycleId": linked_cycle,
            "swapRecordId": swap.id if swap else None,
            "swapStatus": swap.status if swap else None,
            "bidIdpk": swap.source_bid_idpk if swap else None,
            "ledgerEntryIds": ledger_ids, "statementEntryIds": statement_ids,
            "balanceSnapshotIds": snapshot_ids,
            "reconciliationId": reconciliation.id if reconciliation else None,
            "reconciliationStatus": reconciliation.status if reconciliation else None,
        })
    return _page(total, offset, limit, items)


@router.get("/reconciliations", response_model=AuditPage)
def reconciliation_audit(
    cycle_id: str | None = Query(None, alias="cycleId"),
    status: str | None = None,
    offset: int = Query(0, ge=0, le=10000),
    limit: int = Query(50, ge=1, le=100),
    session: Session = Depends(get_session),
) -> AuditPage:
    conditions = []
    if cycle_id is not None:
        conditions.append(WalletReconciliation.cycle_id == cycle_id)
    if status is not None:
        conditions.append(WalletReconciliation.status == status)
    total = session.exec(select(func.count(WalletReconciliation.id)).where(*conditions)).one()
    reports = session.exec(select(WalletReconciliation).where(*conditions)
        .order_by(WalletReconciliation.created_at.desc(), WalletReconciliation.id.desc())
        .offset(offset).limit(limit)).all()
    items = []
    for row in reports:
        source = row.explanation or {}
        local = source.get("local_evidence") or {}
        # La referencia de un ajuste desconocido es opaca: no se publica.
        operation_fields = {
            "cycle_id", "operation", "statement_kinds", "asset",
            "wallet_entry_ids", "ledger_entry_ids", "swap_id", "bid_idpk",
            "wallet_amount", "local_amount", "difference", "status",
        }
        operations = [_safe_fields(operation, operation_fields)
                      for operation in local.get("operations", []) if isinstance(operation, dict)]
        materialization = local.get("ledger_materialization")
        last_close = local.get("last_close")
        single_cycle = local.get("single_cycle_cr")
        items.append({
            "id": row.id, "cycleId": row.cycle_id, "status": row.status,
            "authority": row.authority, "createdAt": row.created_at,
            "cr": {"local": row.cr_available_local, "wallet": row.cr_available_wallet,
                   "difference": row.cr_difference, "explained": row.cr_explained,
                   "unexplained": row.cr_unexplained},
            "kwh": {"local": row.kwh_available_local, "wallet": row.kwh_available_wallet,
                    "difference": row.kwh_difference, "explained": row.kwh_explained,
                    "unexplained": row.kwh_unexplained},
            "walletAsOf": source.get("wallet_as_of"),
            "statementComplete": source.get("statement_complete"),
            "baselineSnapshotId": source.get("baseline_snapshot_id"),
            "operations": operations,
            "ledgerMaterialization": _safe_fields(materialization, {
                "budget_rebuilt", "budget_cycle", "energy_rebuilt", "energy_cycle", "consistent",
            }) if isinstance(materialization, dict) else None,
            "lastClose": _safe_fields(last_close, {"wallet_cr", "cycle_cr", "difference"})
                if isinstance(last_close, dict) else None,
            "singleCycleCr": _safe_fields(single_cycle, {
                "wallet_available_plus_held", "cycle_budget", "difference", "ledger_entry_ids",
            }) if isinstance(single_cycle, dict) else None,
        })
    return _page(total, offset, limit, items)
