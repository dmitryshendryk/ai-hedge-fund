"""Ticker routes: volatility-based position sizing."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.backend.models.sizing_schemas import PositionSizingResponse
from app.backend.services.pricing_service import get_atr, suggest_position_size

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ticker", tags=["ticker"])


@router.get("/{symbol}/sizing", response_model=PositionSizingResponse)
async def sizing_endpoint(
    symbol: str,
    account_value: float = Query(..., description="Total account value in USD"),
    risk_pct: float = Query(0.02, description="Fraction of the account to risk, e.g. 0.02 for 2%"),
) -> PositionSizingResponse:
    sym = (symbol or "").strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="Ticker cannot be empty")

    snapshot = await get_atr(sym)
    if snapshot is None:
        raise HTTPException(status_code=503, detail=f"No price history available for {sym}")

    try:
        sizing = suggest_position_size(
            account_value=account_value,
            risk_pct=risk_pct,
            atr=snapshot.atr,
            price=snapshot.latest_close,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return PositionSizingResponse(
        ticker=sym,
        price=round(snapshot.latest_close, 2),
        atr=round(snapshot.atr, 4),
        atr_pct_of_price=round(snapshot.atr_pct_of_price, 2),
        shares=sizing.shares,
        risk_amount=sizing.risk_amount,
        stop_distance=sizing.stop_distance,
        stop_multiple=sizing.stop_multiple,
        suggested_stop_price=round(snapshot.latest_close - sizing.stop_distance, 2),
        position_value=sizing.position_value,
        position_pct_of_account=sizing.position_pct_of_account,
        capped_shares=sizing.capped_shares,
        capped_by=sizing.capped_by,
    )
