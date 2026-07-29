"""Pydantic schemas for the positions (holdings) page."""

from pydantic import BaseModel


class PositionResponse(BaseModel):
    """A held position enriched with live price + P&L.

    P&L fields are None when yfinance can't price the ticker (delisted, rate
    limited, or in the pricing_service cooldown) — the row still renders with
    its static cost data so the holding never silently disappears.

    `unrealized_pnl_pct` is measured against the user's cost basis;
    `return_since_entry_pct` / `alpha_pct_vs_spy` are measured from the
    entry-date close, so the two can differ when cost basis ≠ that close.
    """

    id: int
    ticker: str
    shares: float
    cost_basis: float  # average cost per share
    entry_date: str
    notes: str | None = None
    cost_value: float  # shares * cost_basis

    current_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    unrealized_pnl_pct: float | None = None
    return_since_entry_pct: float | None = None
    alpha_pct_vs_spy: float | None = None
    price_as_of: str | None = None


class ConcentrationBucket(BaseModel):
    """One sector, industry, or single-name share of portfolio value.

    `tier` compares `weight_pct` against the thresholds in position_service.
    The "Unclassified" bucket is always "ok": broad-market ETFs carry no
    sector in the yfinance payload, and an index fund is not a factor bet.
    """

    name: str
    value: float
    weight_pct: float
    tickers: list[str]
    tier: str  # "ok" | "warn" | "critical"


class PortfolioConcentration(BaseModel):
    """Factor concentration over owned capital, at sector and industry level.

    Sector is too coarse on its own — semiconductors, semicap equipment and
    AI compute all report "Technology" yet de-rate independently — so both
    granularities are reported.

    Positions yfinance cannot price contribute at cost basis rather than being
    dropped; excluding them would understate concentration. `valued_on`
    discloses which basis was used so the UI can caveat the figures.
    """

    total_value: float
    valued_on: str  # "market" | "mixed" | "cost"
    sectors: list[ConcentrationBucket]
    industries: list[ConcentrationBucket]
    positions: list[ConcentrationBucket]
    warnings: list[str]  # most severe first
    unclassified_pct: float
    sector_warn_pct: float
    sector_critical_pct: float
    industry_warn_pct: float
    industry_critical_pct: float


class PositionListResponse(BaseModel):
    """Positions plus portfolio-level roll-ups for the page header."""

    items: list[PositionResponse]
    total: int
    total_cost_value: float
    total_market_value: float | None = None
    total_unrealized_pnl: float | None = None
    total_unrealized_pnl_pct: float | None = None
    concentration: PortfolioConcentration | None = None


class PositionAddRequest(BaseModel):
    ticker: str
    shares: float
    cost_basis: float
    entry_date: str | None = None  # ISO date; defaults to today when omitted
    notes: str | None = None


class PositionUpdateRequest(BaseModel):
    """All fields optional — only the ones provided are updated."""

    shares: float | None = None
    cost_basis: float | None = None
    entry_date: str | None = None
    notes: str | None = None
