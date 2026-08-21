"""yfinance-backed pricing service: period returns + SPY-relative alpha.

Provides ticker close-on-or-after-date lookup, period-return %, and
SPY-relative alpha computation. Used by:
  - watchlist 'return since added' column
  - discovery 'top-N N-day performance' column
  - whale_entry_service current-price lookup
  - discovery_backtest_service trigger-return computation

Caches each (ticker, since_date) computation for 30 min — daily closes don't
change intraday, so a short TTL covers same-day refresh without re-hitting
yfinance for every API call.

Burst protection:
  - Per-key in-flight asyncio.Lock prevents N coroutines from racing past
    the same cache miss (the "thundering herd" — what triggered yfinance
    rate-limiting before).
  - Module-level Semaphore caps concurrent yfinance calls regardless of
    cache state, smoothing bursts across pricing + relative_strength +
    revenue_acceleration + whale_entry_service callers.
"""

import asyncio
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 1800.0
_CACHE_MAX_SIZE = 500
_SPY_TICKER = "SPY"
_MAX_CONCURRENT_YFINANCE_CALLS = 2
_RATE_LIMIT_RETRY_DELAY_S = 5.0
_GLOBAL_COOLDOWN_SECONDS = 60.0


class _RateLimited(Exception):
    """yfinance returned 'Too Many Requests' — transient, do not cache the failure."""


_yfinance_semaphore: asyncio.Semaphore = asyncio.Semaphore(_MAX_CONCURRENT_YFINANCE_CALLS)
_inflight_locks: dict[tuple[str, str], asyncio.Lock] = {}

# Circuit breaker: when yfinance starts throttling, every subsequent call
# returns None immediately for _GLOBAL_COOLDOWN_SECONDS rather than queuing
# more requests that just extend the throttle. The current cache continues
# to serve existing entries; only new fetches short-circuit.
_yfinance_cooldown_until: float = 0.0


def _is_in_cooldown() -> bool:
    return time.monotonic() < _yfinance_cooldown_until


def _trigger_cooldown() -> None:
    global _yfinance_cooldown_until
    _yfinance_cooldown_until = time.monotonic() + _GLOBAL_COOLDOWN_SECONDS
    logger.warning(
        "pricing_service: yfinance rate-limited globally — circuit breaker engaged for %.0fs",
        _GLOBAL_COOLDOWN_SECONDS,
    )


def _is_rate_limit_message(text: str) -> bool:
    lower = text.lower()
    return "too many requests" in lower or "rate limit" in lower


def _compute_rsi(closes: "pd.Series", period: int = 14) -> float | None:
    """Wilder's RSI on a close-price series. None when < period+1 finite closes.

    Uses exponential (Wilder) smoothing rather than a simple rolling mean so
    the value matches what charting tools report.
    """
    series = closes.dropna()
    if len(series) < period + 1:
        return None
    delta = series.diff().dropna()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    if not math.isfinite(rsi):
        return None
    return round(float(rsi), 2)


@dataclass
class PeriodReturn:
    """Two-endpoint price snapshot used to compute return %."""
    start_date: str
    start_price: float
    end_date: str
    end_price: float


@dataclass
class AlphaMetrics:
    """Period-return + SPY alpha for a single ticker."""
    ticker: str
    period_return_pct: float
    spy_return_pct: float
    alpha_pct: float
    start_date: str
    end_date: str
    start_price: float
    end_price: float


_period_cache: OrderedDict[tuple[str, str], tuple[PeriodReturn | None, float]] = OrderedDict()


def _cache_get(key: tuple[str, str]) -> PeriodReturn | None:
    entry = _period_cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _period_cache.pop(key, None)
        return None
    return value


def _cache_put(key: tuple[str, str], value: PeriodReturn | None) -> None:
    _period_cache[key] = (value, time.monotonic())
    while len(_period_cache) > _CACHE_MAX_SIZE:
        _period_cache.popitem(last=False)


def _coerce_since(since: date | datetime | str) -> date:
    if isinstance(since, datetime):
        return since.astimezone(timezone.utc).date() if since.tzinfo else since.date()
    if isinstance(since, date):
        return since
    if isinstance(since, str):
        return date.fromisoformat(since.split("T")[0])
    raise TypeError(f"Unsupported since type: {type(since).__name__}")


def _fetch_period_sync(ticker: str, since: date) -> PeriodReturn | None:
    """Synchronous yfinance fetch. Raises _RateLimited on transient throttling
    (caller decides whether to retry / skip caching); returns None for any
    other failure mode (delisted, malformed data, network).

    Drops trailing rows whose Close is NaN (yfinance often returns an open
    bar for the current trading day before the close print lands, which
    would propagate NaN into every downstream return calculation)."""
    end = date.today() + timedelta(days=1)
    if since >= end:
        return None
    try:
        history = yf.Ticker(ticker).history(start=since, end=end, auto_adjust=True)
    except Exception as exc:
        msg = str(exc)
        if _is_rate_limit_message(msg):
            raise _RateLimited(msg) from exc
        logger.warning("pricing_service yfinance failed for %s: %s", ticker, exc)
        return None
    if history is None or history.empty:
        return None

    try:
        closed = history.dropna(subset=["Close"])
    except KeyError:
        return None
    if closed.empty:
        return None

    first_idx = closed.index[0]
    last_idx = closed.index[-1]
    try:
        start_close = float(closed.iloc[0]["Close"])
        end_close = float(closed.iloc[-1]["Close"])
    except (KeyError, ValueError, TypeError):
        return None
    if start_close <= 0 or end_close <= 0:
        return None
    if not (math.isfinite(start_close) and math.isfinite(end_close)):
        return None
    return PeriodReturn(
        start_date=first_idx.strftime("%Y-%m-%d"),
        start_price=start_close,
        end_date=last_idx.strftime("%Y-%m-%d"),
        end_price=end_close,
    )


async def get_period_return(ticker: str, since: date | datetime | str) -> PeriodReturn | None:
    """Cached: fetch the close on/after `since` and the latest close.

    Returns None on delisted ticker, future date, or network failure.

    Per-key in-flight lock prevents the thundering-herd burst: when N
    coroutines ask for the same (ticker, since) simultaneously, only one
    hits yfinance — the rest await and read from cache.
    """
    since_d = _coerce_since(since)
    cache_key = (ticker.upper(), since_d.isoformat())
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if _is_in_cooldown():
        # Yfinance is currently throttling us — short-circuit without making
        # the request, return None so callers degrade gracefully. Cache
        # entries written before cooldown remain valid.
        return None

    lock = _inflight_locks.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
        if _is_in_cooldown():
            return None
        async with _yfinance_semaphore:
            try:
                result = await asyncio.to_thread(_fetch_period_sync, ticker.upper(), since_d)
            except _RateLimited as exc:
                logger.info(
                    "pricing_service: rate-limited on %s, backing off %.1fs and retrying once",
                    ticker, _RATE_LIMIT_RETRY_DELAY_S,
                )
                await asyncio.sleep(_RATE_LIMIT_RETRY_DELAY_S)
                try:
                    result = await asyncio.to_thread(_fetch_period_sync, ticker.upper(), since_d)
                except _RateLimited:
                    _trigger_cooldown()
                    return None
        _cache_put(cache_key, result)
        return result


async def compute_alpha(ticker: str, since: date | datetime | str) -> AlphaMetrics | None:
    """Compute period return + SPY-relative alpha for one ticker.

    Serial (SPY first, then ticker) rather than parallel: SPY is shared
    across all callers, so a sequential pattern hits the SPY cache after
    the first request and never gets duplicated work.
    """
    since_d = _coerce_since(since)
    spy_data = await get_period_return(_SPY_TICKER, since_d)
    ticker_data = await get_period_return(ticker, since_d)
    if ticker_data is None or spy_data is None:
        return None
    # _fetch_period_sync already rejects start_price <= 0, but guard again
    # in case a future cache entry slips through with a zero — division by
    # zero produces inf which then crashes JSON serialization downstream.
    if ticker_data.start_price <= 0 or spy_data.start_price <= 0:
        return None

    ticker_return_pct = (ticker_data.end_price / ticker_data.start_price - 1.0) * 100.0
    spy_return_pct = (spy_data.end_price / spy_data.start_price - 1.0) * 100.0
    if not (math.isfinite(ticker_return_pct) and math.isfinite(spy_return_pct)):
        return None
    return AlphaMetrics(
        ticker=ticker.upper(),
        period_return_pct=ticker_return_pct,
        spy_return_pct=spy_return_pct,
        alpha_pct=ticker_return_pct - spy_return_pct,
        start_date=ticker_data.start_date,
        end_date=ticker_data.end_date,
        start_price=ticker_data.start_price,
        end_price=ticker_data.end_price,
    )


async def compute_alpha_batch(items: list[tuple[str, date | datetime | str]]) -> dict[str, AlphaMetrics | None]:
    """Compute alpha for many (ticker, since) pairs concurrently.

    Pre-warms the SPY cache once per unique since_date before fanning out
    ticker fetches. Without this, N coroutines each fire their own SPY
    request before the first one populates the cache — the textbook
    thundering-herd pattern that trips yfinance rate-limiting.
    """
    if not items:
        return {}
    unique_dates = {_coerce_since(s).isoformat() for _, s in items}
    for iso in unique_dates:
        await get_period_return(_SPY_TICKER, iso)

    tasks = [compute_alpha(t, s) for t, s in items]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, AlphaMetrics | None] = {}
    for (ticker, _since), res in zip(items, results, strict=True):
        if isinstance(res, BaseException):
            logger.debug("pricing_service: alpha compute failed for %s: %s", ticker, res)
            out[ticker.upper()] = None
        else:
            out[ticker.upper()] = res
    return out


@dataclass
class SmaCross:
    """Latest close vs N-day simple moving average."""
    ticker: str
    latest_close: float
    sma: float
    pct_above_sma: float  # negative when close < SMA (breakdown)


def _compute_sma_sync(ticker: str, days: int) -> SmaCross | None:
    """Fetch ~days+10 calendar days of bars, drop incomplete trailing rows,
    average the last `days` closes, compare to the latest finite close."""
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6) + 10)  # buffer for weekends/holidays
    try:
        history = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        msg = str(exc)
        if _is_rate_limit_message(msg):
            raise _RateLimited(msg) from exc
        logger.warning("pricing_service SMA failed for %s: %s", ticker, exc)
        return None
    if history is None or history.empty:
        return None
    try:
        closed = history.dropna(subset=["Close"])
    except KeyError:
        return None
    if len(closed) < days:
        return None

    closes = closed["Close"].tail(days)
    try:
        sma = float(closes.mean())
        latest = float(closes.iloc[-1])
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(sma) and math.isfinite(latest)) or sma <= 0:
        return None
    return SmaCross(
        ticker=ticker.upper(),
        latest_close=latest,
        sma=sma,
        pct_above_sma=(latest / sma - 1.0) * 100.0,
    )


async def get_sma_cross(ticker: str, days: int = 200) -> SmaCross | None:
    """Cached: latest close vs N-day SMA. Used by the technical_breakdown
    exit-alert rule. Shares the global yfinance cooldown / semaphore guard.
    """
    if _is_in_cooldown():
        return None
    async with _yfinance_semaphore:
        try:
            return await asyncio.to_thread(_compute_sma_sync, ticker.upper(), days)
        except _RateLimited:
            _trigger_cooldown()
            return None


@dataclass
class TechnicalSnapshot:
    """Single-fetch technical read for exhaustion detectors."""
    ticker: str
    latest_close: float
    sma200: float
    pct_above_sma: float  # (close/sma - 1) * 100; negative below trend
    rsi14: float | None


def _compute_technical_snapshot_sync(ticker: str, days: int) -> TechnicalSnapshot | None:
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=int(days * 1.6) + 30)
    try:
        history = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        msg = str(exc)
        if _is_rate_limit_message(msg):
            raise _RateLimited(msg) from exc
        logger.warning("pricing_service technical snapshot failed for %s: %s", ticker, exc)
        return None
    if history is None or history.empty:
        return None
    try:
        closed = history.dropna(subset=["Close"])
    except KeyError:
        return None
    if len(closed) < days:
        return None

    closes = closed["Close"]
    window = closes.tail(days)
    try:
        sma = float(window.mean())
        latest = float(closes.iloc[-1])
    except (ValueError, TypeError):
        return None
    if not (math.isfinite(sma) and math.isfinite(latest)) or sma <= 0:
        return None
    return TechnicalSnapshot(
        ticker=ticker.upper(),
        latest_close=latest,
        sma200=sma,
        pct_above_sma=(latest / sma - 1.0) * 100.0,
        rsi14=_compute_rsi(closes),
    )


async def get_technical_snapshot(ticker: str, sma_days: int = 200) -> TechnicalSnapshot | None:
    """Single-fetch snapshot: latest close, N-day SMA + extension, and RSI(14).
    Used by the technical_exhaustion Devil's Advocate detector. Shares the
    global yfinance cooldown / semaphore guard.
    """
    if _is_in_cooldown():
        return None
    async with _yfinance_semaphore:
        try:
            return await asyncio.to_thread(_compute_technical_snapshot_sync, ticker.upper(), sma_days)
        except _RateLimited:
            _trigger_cooldown()
            return None


# Stop distance as a multiple of ATR. 1.5x sits outside normal daily noise, so a
# touch signals the move failed rather than that the name simply wobbled.
_SIZING_STOP_MULTIPLE: float = 1.5
_ATR_PERIOD: int = 14


def _compute_atr(history: "pd.DataFrame", period: int = _ATR_PERIOD) -> float | None:
    """Wilder's Average True Range over an OHLC frame.

    True range spans the prior close, so an overnight gap counts as volatility
    instead of hiding behind a narrow intraday spread.

    Returns:
        None when High/Low/Close are absent or fewer than period+1 bars remain.
    """
    required = ("High", "Low", "Close")
    if history is None or any(col not in history.columns for col in required):
        return None
    frame = history.dropna(subset=list(required))
    if len(frame) < period + 1:
        return None

    high = frame["High"]
    low = frame["Low"]
    prev_close = frame["Close"].shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1).dropna()
    if len(true_range) < period:
        return None

    atr = float(true_range.ewm(alpha=1.0 / period, adjust=False).mean().iloc[-1])
    return atr if math.isfinite(atr) and atr >= 0 else None


@dataclass
class AtrSnapshot:
    """Latest close plus ATR, for sizing and stop placement."""
    ticker: str
    latest_close: float
    atr: float
    atr_pct_of_price: float  # daily range as a share of price


def _compute_atr_sync(ticker: str, period: int) -> AtrSnapshot | None:
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=period * 6 + 30)
    try:
        history = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    except Exception as exc:
        msg = str(exc)
        if _is_rate_limit_message(msg):
            raise _RateLimited(msg) from exc
        logger.warning("pricing_service ATR failed for %s: %s", ticker, exc)
        return None
    if history is None or history.empty:
        return None

    atr = _compute_atr(history, period)
    if atr is None or atr <= 0:
        return None
    try:
        latest = float(history["Close"].dropna().iloc[-1])
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    if not math.isfinite(latest) or latest <= 0:
        return None

    return AtrSnapshot(
        ticker=ticker.upper(),
        latest_close=latest,
        atr=atr,
        atr_pct_of_price=atr / latest * 100.0,
    )


async def get_atr(ticker: str, period: int = _ATR_PERIOD) -> AtrSnapshot | None:
    """ATR read sharing the global yfinance cooldown / semaphore guard."""
    if _is_in_cooldown():
        return None
    async with _yfinance_semaphore:
        try:
            return await asyncio.to_thread(_compute_atr_sync, ticker.upper(), period)
        except _RateLimited:
            _trigger_cooldown()
            return None


@dataclass
class PositionSizing:
    """How many shares a fixed dollar risk permits at a name's volatility.

    `shares` answers the risk question alone and can exceed the account for a
    low-volatility name, so `capped_shares` also respects available capital and
    `capped_by` names the binding constraint.
    """
    shares: int
    risk_amount: float
    stop_distance: float
    stop_multiple: float
    position_value: float | None
    position_pct_of_account: float | None
    capped_shares: int
    capped_by: str  # "risk" | "capital"


def suggest_position_size(
    account_value: float,
    risk_pct: float,
    atr: float,
    price: float | None = None,
    stop_multiple: float = _SIZING_STOP_MULTIPLE,
) -> PositionSizing:
    """Shares permitted by a fixed dollar risk at the name's daily range.

    Args:
        risk_pct: Fraction of the account to risk, e.g. 0.02 for 2%.
        atr: Average True Range in currency units, not percent.
        price: Latest close, supplied only to report the resulting exposure.

    Returns:
        shares rounded down, so the risk budget is never exceeded.

    Raises:
        ValueError: account_value not positive, or risk_pct outside (0, 1].
    """
    if account_value <= 0:
        raise ValueError("Account value must be positive")
    if not 0 < risk_pct <= 1:
        raise ValueError("Risk percent must be between 0 and 1")

    risk_amount = account_value * risk_pct
    stop_distance = stop_multiple * atr
    shares = int(risk_amount // stop_distance) if stop_distance > 0 else 0

    position_value = shares * price if price is not None else None
    affordable = int(account_value // price) if price is not None and price > 0 else shares
    capped_shares = min(shares, affordable)
    return PositionSizing(
        shares=shares,
        risk_amount=round(risk_amount, 2),
        stop_distance=round(stop_distance, 4),
        stop_multiple=stop_multiple,
        position_value=round(position_value, 2) if position_value is not None else None,
        position_pct_of_account=round(position_value / account_value * 100.0, 2) if position_value is not None else None,
        capped_shares=capped_shares,
        capped_by="capital" if capped_shares < shares else "risk",
    )
