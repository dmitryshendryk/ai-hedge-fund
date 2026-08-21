"""Schemas for volatility-based position sizing."""

from pydantic import BaseModel


class PositionSizingResponse(BaseModel):
    """Shares a fixed dollar risk permits, given the name's daily range.

    `atr` and `stop_distance` are in currency units, not percent. `shares` is
    rounded down so the risk budget is never exceeded.
    """

    ticker: str
    price: float
    atr: float
    atr_pct_of_price: float
    shares: int
    risk_amount: float
    stop_distance: float
    stop_multiple: float
    suggested_stop_price: float
    position_value: float | None = None
    position_pct_of_account: float | None = None
    capped_shares: int = 0
    capped_by: str = "risk"  # "risk" | "capital"
