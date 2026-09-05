"""Discovery source: share count reduction (the 'Share Cannibal' signal).

A company that reduces shares outstanding manufactures EPS growth without
needing topline lift. Apple's ~25% share reduction over the last decade is
the canonical example — it's a multi-year tailwind hidden inside cash-flow
discipline.

Universe = watchlist + top 50 from recent DiscoverySnapshot (matches the
explicit user call). yfinance.get_shares_full(start=...) returns a daily
share-count series sourced from the most recent 10-Q/K filings, so the YoY
comparison is grounded in filed numbers, not estimates.

Score tiers (annual share count reduction):
  - +15: ≥3% YoY reduction (qualifying buyback)
  - +25: ≥5% YoY reduction (aggressive)
  - +35: ≥7% YoY reduction (rare — usually deep-value or post-LBO)

Gracefully drops tickers where yfinance can't supply at least one share
count in each of the two windows (newly-listed companies, ADRs).
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import yfinance as yf
from sqlalchemy import func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal

logger = logging.getLogger(__name__)

_QUALIFYING_PCT = 0.03
_AGGRESSIVE_PCT = 0.05
_EXTREME_PCT = 0.07

_UNIVERSE_LOOKBACK_HOURS = 48
_MAX_UNIVERSE_SIZE = 200  # widened in Tier 2 universe-cap pass

_LOOKBACK_DAYS = 380           # pull > 1y to guarantee a full 12-mo comparison window
_PRIOR_WINDOW_NEAR_DAYS = 330  # 11 months — wide enough to catch a filing
_PRIOR_WINDOW_FAR_DAYS = 400   # 13 months — bounded so we don't compare against 2-year-old data

_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h — share counts only refresh on quarterly filings
_CACHE_MAX_SIZE = 500


class _CacheSentinel(Enum):
    """Distinct from None — None is a legal cached value (ticker with no
    usable share series). MISS means "never computed or expired".
    """
    MISS = "miss"


_MISS = _CacheSentinel.MISS


@dataclass(frozen=True)
class ShareReduction:
    """Result of the YoY share-count delta for one ticker."""
    ticker: str
    current_shares: int
    prior_shares: int
    reduction_pct: float  # positive = reduction (buyback); negative = dilution
    prior_as_of: str      # ISO date of the prior-window observation used


_cache: OrderedDict[str, tuple[ShareReduction | None, float]] = OrderedDict()


def _cache_get(key: str) -> ShareReduction | None | _CacheSentinel:
    entry = _cache.get(key)
    if entry is None:
        return _MISS
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return _MISS
    return value


def _cache_put(key: str, value: ShareReduction | None) -> None:
    _cache[key] = (value, time.monotonic())
    while len(_cache) > _CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _gather_universe() -> list[str]:
    db = SessionLocal()
    try:
        watchlist = {row[0].upper() for row in db.query(WatchlistItem.ticker).all() if row[0]}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_UNIVERSE_LOOKBACK_HOURS)
        snapshots = {
            row[0].upper()
            for row in (
                db.query(DiscoverySnapshot.ticker)
                .filter(DiscoverySnapshot.snapshot_at >= cutoff)
                .filter(DiscoverySnapshot.is_ticker == True)  # noqa: E712
                .group_by(DiscoverySnapshot.ticker)
                .order_by(func.max(DiscoverySnapshot.score).desc())
                .limit(_MAX_UNIVERSE_SIZE)
                .all()
            )
            if row[0]
        }
    finally:
        db.close()
    return sorted(watchlist | snapshots)[:_MAX_UNIVERSE_SIZE]


def _normalize_index_to_naive_utc(series):
    """yfinance share series sometimes returns a tz-aware DatetimeIndex and
    sometimes a naive one. Coerce to naive UTC for boolean-mask comparison
    against naive datetimes downstream.
    """
    try:
        return series.index.tz_convert(None)
    except (TypeError, AttributeError):
        return series.index


def _compute_reduction_sync(ticker: str) -> ShareReduction | None:
    """Pull the share-count series and compute YoY delta. Returns None if
    yfinance can't supply at least one observation in each window.
    """
    try:
        t = yf.Ticker(ticker.upper())
        start = (datetime.now(timezone.utc) - timedelta(days=_LOOKBACK_DAYS)).date().isoformat()
        series = t.get_shares_full(start=start)
    except Exception as exc:
        logger.debug("share_cannibal: yfinance shares fetch failed for %s: %s", ticker, exc)
        return None

    if series is None or len(series) == 0:
        return None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    prior_start = now - timedelta(days=_PRIOR_WINDOW_FAR_DAYS)
    prior_end = now - timedelta(days=_PRIOR_WINDOW_NEAR_DAYS)

    idx = _normalize_index_to_naive_utc(series)
    prior_window = series[(idx >= prior_start) & (idx <= prior_end)]
    if len(prior_window) == 0:
        return None

    prior_shares = int(prior_window.iloc[-1])
    current_shares = int(series.iloc[-1])
    if prior_shares <= 0 or current_shares <= 0:
        return None

    reduction_pct = (prior_shares - current_shares) / prior_shares
    prior_as_of_raw = prior_window.index[-1]
    try:
        prior_as_of = prior_as_of_raw.strftime("%Y-%m-%d")
    except AttributeError:
        prior_as_of = str(prior_as_of_raw)[:10]

    return ShareReduction(
        ticker=ticker.upper(),
        current_shares=current_shares,
        prior_shares=prior_shares,
        reduction_pct=reduction_pct,
        prior_as_of=prior_as_of,
    )


async def _get_reduction(ticker: str) -> ShareReduction | None:
    cached = _cache_get(ticker)
    if cached is not _MISS:
        assert not isinstance(cached, _CacheSentinel)
        return cached
    result = await asyncio.to_thread(_compute_reduction_sync, ticker)
    _cache_put(ticker, result)
    return result


async def fetch() -> list[tuple[str, IdeaSignal]]:
    universe = _gather_universe()
    if not universe:
        return []

    # Bounded concurrency: yfinance pulls a share index per call,
    # don't blow through rate limits.
    sem = asyncio.Semaphore(4)

    async def _gated(t: str) -> ShareReduction | None:
        async with sem:
            return await _get_reduction(t)

    results = await asyncio.gather(*(_gated(t) for t in universe), return_exceptions=True)

    out: list[tuple[str, IdeaSignal]] = []
    for ticker, result in zip(universe, results, strict=True):
        if isinstance(result, BaseException):
            logger.debug("share_cannibal: error for %s: %s", ticker, result)
            continue
        if result is None or result.reduction_pct < _QUALIFYING_PCT:
            continue

        if result.reduction_pct >= _EXTREME_PCT:
            score = 35.0
            tier = "extreme buyback"
        elif result.reduction_pct >= _AGGRESSIVE_PCT:
            score = 25.0
            tier = "aggressive buyback"
        else:
            score = 15.0
            tier = "qualifying buyback"

        pct_display = result.reduction_pct * 100
        label = (
            f"{tier}: {result.prior_shares:,} → {result.current_shares:,} "
            f"shares ({pct_display:.1f}% reduction YoY)"
        )

        out.append((ticker, IdeaSignal(
            source="share_cannibal",
            score=score,
            label=label,
            detail={
                "ticker": ticker,
                "current_shares": result.current_shares,
                "prior_shares": result.prior_shares,
                "reduction_pct": result.reduction_pct,
                "prior_as_of": result.prior_as_of,
                "tier": tier,
            },
        )))
    return out
