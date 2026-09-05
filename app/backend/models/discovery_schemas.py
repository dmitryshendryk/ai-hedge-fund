"""Pydantic schemas for the Discovery (aggregated ideas) feature."""

from typing import Any

from pydantic import BaseModel


class IdeaSignal(BaseModel):
    source: str            # "spinoff" | "csuite_buy" | "squeeze" (extensible)
    score: float           # contribution to total score
    label: str             # human-readable, e.g. "CEO bought $1.5M"
    detail: dict[str, Any] | None = None  # source-specific metadata
    kill_filter: bool = False  # if True, the engine drops this ticker entirely


class DiscoveryIdea(BaseModel):
    ticker: str            # ticker symbol or CIK string (when no ticker yet)
    company: str | None = None
    cik: int | None = None
    score: float
    signals: list[IdeaSignal]
    is_ticker: bool = True  # False if `ticker` field actually holds a CIK
    sector: str | None = None  # yfinance Ticker.info["sector"], populated by the alpha enricher
    industry: str | None = None  # finer than sector: semis vs semicap de-rate independently
    return_30d_pct: float | None = None
    alpha_30d_pct: float | None = None
    distance_from_whale_entry_pct: float | None = None
    pct_above_sma: float | None = None  # percent, not a fraction; None when unpriceable
    exhaustion_penalty: float = 0.0  # points already subtracted from score


class SectorBreakdown(BaseModel):
    """Aggregated cumulative score for one sector across the top ideas."""
    sector: str
    score_total: float
    score_pct: float  # share of the cumulative top-N score
    ticker_count: int
    top_tickers: list[str]


class DiscoveryConcentration(BaseModel):
    """Top-of-Discovery sector mix + an overcrowding warning list."""
    sectors: list[SectorBreakdown]
    overcrowding_threshold_pct: float
    overcrowding_sectors: list[str]
    unclassified_pct: float  # share of score with no known sector


class MacroRegimeSnapshot(BaseModel):
    """Current macro 'weather' applied to Discovery scoring."""
    mode: str  # "risk_on" | "risk_off"
    score_multiplier: float  # 1.0 on risk_on, 0.3 on risk_off
    reasons: list[str]  # empty in risk_on
    metrics: dict[str, float | None]  # yield_curve_10y_2y, vix, hy_oas
    as_of: str | None = None  # ISO date of most recent FRED observation


class DiscoveryResponse(BaseModel):
    ideas: list[DiscoveryIdea]
    total: int  # total ideas in the cached universe BEFORE pagination
    cached: bool
    generated_at: str  # ISO timestamp
    concentration: DiscoveryConcentration | None = None
    macro_regime: MacroRegimeSnapshot | None = None
    page: int = 1
    page_size: int = 50
    total_pages: int = 1
    has_more: bool = False


class DiscoveryCacheFlushResponse(BaseModel):
    """What a Discovery cache flush actually discarded.

    Mirrors the platform-wide POST /cache/flush shape so one client handler
    covers both. Counts are zero when nothing was cached, which the UI reports
    rather than claiming a refresh happened.
    """
    cleared: dict[str, int]
    total_entries: int
    cache_ttl_seconds: float  # how long the next compute stays cached


class DiscoverySnapshotItem(BaseModel):
    """One historical snapshot of a ticker's Discovery score."""
    ticker: str
    cik: int | None = None
    company: str | None = None
    score: float
    distinct_sources: int
    snapshot_at: str  # ISO timestamp


class DiscoveryHistoryResponse(BaseModel):
    """Time series of snapshots for a single ticker."""
    ticker: str
    snapshots: list[DiscoverySnapshotItem]
    total: int


class DiscoveryMover(BaseModel):
    """A ticker whose Discovery score changed materially over the lookback window."""
    ticker: str
    cik: int | None = None
    company: str | None = None
    score_now: float
    score_before: float
    delta: float
    snapshot_at_now: str
    snapshot_at_before: str


class DiscoveryMoversResponse(BaseModel):
    movers: list[DiscoveryMover]
    days: int
    total: int
