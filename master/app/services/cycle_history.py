"""E2-056 / RF01: historial de mercado por ciclo, exclusivamente de lectura.

Cada sección se deriva de registros persistidos. Una respuesta HTTP 202 de wallet,
la publicación de un mensaje y el settlement son hechos diferentes.
No se exponen swapToken, llaves de idempotencia ni payloads sensibles.
"""

from sqlmodel import Session, select

from ..models import (
    Cycle, ImbalanceSettlement, LedgerEntry, MarketBid, MarketOffer,
    NegotiationReport, OutboundMessage, SwapRecord, TradeRecord,
)
from ..schemas import (
    CycleHistoryOut, CycleHistoryOfferOut, CycleHistoryOfferRevisionOut,
    CycleHistoryBidOut, CycleHistorySwapOut, CycleHistoryTradeOut,
    CycleHistorySettlementOut, CycleHistoryEffectOut, NegotiationReportOut,
)


def get_cycle_history(session: Session, cycle_id: str) -> CycleHistoryOut | None:
    """Devuelve None para un ciclo inexistente, sin modificar la sesión."""
    cycle = session.get(Cycle, cycle_id)
    if cycle is None:
        return None

    entries = session.exec(
        select(LedgerEntry).where(LedgerEntry.cycle_id == cycle_id)
        .order_by(LedgerEntry.sequence_no, LedgerEntry.id)
    ).all()
    offers = session.exec(
        select(MarketOffer).where(MarketOffer.cycle_id == cycle_id)
        .order_by(MarketOffer.published_at, MarketOffer.id)
    ).all()
    revisions = session.exec(
        select(OutboundMessage).where(
            OutboundMessage.cycle_id == cycle_id,
            OutboundMessage.message_type.in_(("offer", "offer-updated", "offer-withdrawn")),
        ).order_by(OutboundMessage.created_at, OutboundMessage.id)
    ).all()
    bids = session.exec(
        select(MarketBid).where(MarketBid.cycle_id == cycle_id)
        .order_by(MarketBid.created_at, MarketBid.id)
    ).all()
    swaps = session.exec(
        select(SwapRecord).where(SwapRecord.cycle_id == cycle_id)
        .order_by(SwapRecord.created_at, SwapRecord.id)
    ).all()
    trades = session.exec(
        select(TradeRecord).where(TradeRecord.cycle_id == cycle_id)
        .order_by(TradeRecord.recorded_at, TradeRecord.id)
    ).all()
    settlements = session.exec(
        select(ImbalanceSettlement).where(ImbalanceSettlement.cycle_id == cycle_id)
        .order_by(ImbalanceSettlement.recorded_at, ImbalanceSettlement.id)
    ).all()
    reports = session.exec(
        select(NegotiationReport).where(NegotiationReport.cycle_id == cycle_id)
        .order_by(NegotiationReport.created_at, NegotiationReport.id)
    ).all()

    effects = [CycleHistoryEffectOut(
        sequence=e.sequence_no, operationType=e.operation_type,
        idpk=e.idpk, sourceMsgId=e.source_msg_id,
        budgetDelta=e.budget_delta, energyDelta=e.energy_delta,
        budgetAfter=e.budget_after, energyAfter=e.energy_after,
        appliedAt=e.applied_at, details=e.details,
    ) for e in entries]

    return CycleHistoryOut(
        cycleId=cycle.cycle_id,
        createdAt=cycle.created_at,
        validUntil=cycle.valid_until,
        schedulerState=cycle.scheduler_state,
        closedAt=cycle.closed_at,
        strategyVersion=cycle.strategy_version,
        marketOpensAt=cycle.market_opens_at,
        marketLastOfferAt=cycle.market_last_offer_at,
        marketLastBidAt=cycle.market_last_bid_at,
        marketClosesAt=cycle.market_closes_at,
        statusMsgId=cycle.status_msg_id,
        statusIdpk=cycle.status_idpk,
        statusStatement=cycle.status_payload or {},
        funding=[e for e in effects if e.operationType == "TRANSFER_IN"],
        offers=[CycleHistoryOfferOut(
            offerId=o.offer_id, cityId=o.city_id, isLocal=o.is_local,
            initialQuantity=o.initial_quantity, remaining=o.remaining_quantity,
            pricePerEnergy=o.price_per_energy, status=o.status,
            publishedAt=o.published_at, lastUpdatedAt=o.last_updated_at,
            withdrawnAt=o.withdrawn_at,
        ) for o in offers],
        offerRevisions=[CycleHistoryOfferRevisionOut(
            type=r.message_type, msgId=r.msg_id, idpk=r.idpk,
            offerId=(r.payload.get("data") or {}).get("offerId")
            if isinstance(r.payload.get("data"), dict) else None,
            remaining=(r.payload.get("data") or {}).get("remaining")
            if isinstance(r.payload.get("data"), dict) else None,
            pricePerEnergy=(r.payload.get("data") or {}).get("pricePerEnergy")
            if isinstance(r.payload.get("data"), dict) else None,
            publicationStatus=r.status, createdAt=r.created_at,
            publishedAt=r.published_at,
        ) for r in revisions],
        bids=[CycleHistoryBidOut(
            idpk=b.idpk, msgId=b.msg_id, direction=b.direction,
            offerId=b.offer_id, counterpartCityId=b.counterpart_city_id,
            quantity=b.quantity, status=b.status,
            rejectionReason=b.rejection_reason,
            availableOnRejection=b.available_on_rejection,
            acceptedPricePerEnergy=b.accepted_price_per_energy,
            swapId=b.swap_id, createdAt=b.created_at, respondedAt=b.responded_at,
        ) for b in bids],
        swaps=[CycleHistorySwapOut(
            idpk=s.idpk, swapId=s.swap_id, role=s.role,
            counterpartCityId=s.counterpart_city_id,
            giveAsset=s.give_asset, giveAmount=s.give_amount,
            takeAsset=s.take_asset, takeAmount=s.take_amount,
            memo=s.memo, status=s.status,
            rejectionReason=s.rejection_reason,
            sourceBidIdpk=s.source_bid_idpk,
            proposalHttpAcceptedAt=s.proposal_http_accepted_at,
            expiresAt=s.expires_at, createdAt=s.created_at, settledAt=s.settled_at,
        ) for s in swaps],
        trades=[CycleHistoryTradeOut(
            swapId=t.swap_id, offerId=t.offer_id, role=t.role,
            seller=t.seller, buyer=t.buyer, quantity=t.quantity,
            pricePerEnergy=t.price_per_energy, energyTotal=t.energy_total,
            transportPerEnergy=t.transport_per_energy,
            transportTotal=t.transport_total, routeEnabled=t.route_enabled,
            recordedAt=t.recorded_at,
        ) for t in trades],
        settlements=[CycleHistorySettlementOut(
            settlementId=s.settlement_id, coverage=s.coverage,
            kwhHeld=s.kwh_held, deficit=s.deficit,
            pricePerEnergy=s.price_per_energy, total=s.total,
            balanceAfter=s.balance_after, recordedAt=s.recorded_at,
        ) for s in settlements],
        negotiationReports=[NegotiationReportOut(
            status=r.status, reason=r.reason, msgId=r.msg_id, idpk=r.idpk,
            budgetBalance=r.budget_balance, energyBalance=r.energy_balance,
            createdAt=r.created_at, sentAt=r.sent_at,
        ) for r in reports],
        lastOperation=effects[-1] if effects else None,
        ledger=effects,
        budgetBalance=cycle.budget_balance,
        energyBalance=cycle.energy_balance,
    )
