"""Quality + valuation + dividend metrics from yfinance .info (single-call shape)."""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

import yfinance as yf

from ._helpers import (
    CACHE_MAX_SIZE,
    CACHE_TTL_SECONDS,
    consecutive_dividend_growth_years,
    safe_float,
)

logger = logging.getLogger(__name__)


@dataclass
class CompanyMetrics:
    """Quality / valuation / cash / dividend snapshot for one ticker.

    All fields default None when yfinance can't supply the metric (delisted,
    untracked, or missing line item). Sources read whatever subset they need.
    """
    ticker: str
    # Display
    long_name: str | None = None
    sector: str | None = None
    # Quality
    return_on_equity: float | None = None
    return_on_assets: float | None = None
    debt_to_equity: float | None = None
    gross_margin: float | None = None
    profit_margin: float | None = None
    # Valuation
    trailing_pe: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    price_to_book: float | None = None
    target_mean_price: float | None = None
    # Cash generation
    free_cash_flow: float | None = None
    market_cap: float | None = None
    fcf_yield: float | None = None
    # Dividends
    current_dividend_yield: float | None = None
    consecutive_dividend_growth_years: int = 0
    # Workforce
    full_time_employees: int | None = None
    has_data: bool = False


_cache: OrderedDict[str, tuple[CompanyMetrics | None, float]] = OrderedDict()


def _cache_get(key: str) -> CompanyMetrics | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: CompanyMetrics | None) -> None:
    _cache[key] = (value, time.monotonic())
    while len(_cache) > CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _build_metrics_from_info(ticker: str, info: dict, ticker_obj: yf.Ticker | None = None) -> CompanyMetrics:
    """Build CompanyMetrics from yfinance info dict.

    Args:
        ticker: Stock ticker symbol
        info: yfinance Ticker.info dict
        ticker_obj: yfinance Ticker object (optional; used for dividend history)

    Returns:
        CompanyMetrics instance with all available fields populated
    """
    fcf = safe_float(info.get("freeCashflow"))
    market_cap = safe_float(info.get("marketCap"))
    fcf_yield: float | None = None
    if fcf is not None and market_cap and market_cap > 0:
        fcf_yield = fcf / market_cap

    dividend_years = 0
    if ticker_obj is not None:
        dividend_years = consecutive_dividend_growth_years(ticker_obj)

    metrics = CompanyMetrics(
        ticker=ticker.upper(),
        long_name=(info.get("longName") or info.get("shortName") or None),
        sector=(info.get("sector") or None),
        return_on_equity=safe_float(info.get("returnOnEquity")),
        return_on_assets=safe_float(info.get("returnOnAssets")),
        debt_to_equity=safe_float(info.get("debtToEquity")),
        gross_margin=safe_float(info.get("grossMargins")),
        profit_margin=safe_float(info.get("profitMargins")),
        trailing_pe=safe_float(info.get("trailingPE")),
        forward_pe=safe_float(info.get("forwardPE")),
        peg_ratio=safe_float(info.get("trailingPegRatio") or info.get("pegRatio")),
        price_to_book=safe_float(info.get("priceToBook")),
        target_mean_price=safe_float(info.get("targetMeanPrice")),
        free_cash_flow=fcf,
        market_cap=market_cap,
        fcf_yield=fcf_yield,
        current_dividend_yield=safe_float(info.get("dividendYield")),
        consecutive_dividend_growth_years=dividend_years,
        full_time_employees=(int(employees) if (employees := safe_float(info.get("fullTimeEmployees"))) and employees > 0 else None),
    )
    metrics.has_data = any(
        v is not None for v in (
            metrics.return_on_equity, metrics.trailing_pe,
            metrics.current_dividend_yield, metrics.gross_margin,
            metrics.fcf_yield,
        )
    )
    return metrics


def _fetch_company_metrics_sync(ticker: str) -> CompanyMetrics | None:
    try:
        t = yf.Ticker(ticker.upper())
        info = t.info
    except Exception as exc:
        logger.debug("fundamentals: yfinance .info failed for %s: %s", ticker, exc)
        return None
    if not info or not isinstance(info, dict):
        return None

    return _build_metrics_from_info(ticker, info, t)


async def get_company_metrics(ticker: str) -> CompanyMetrics | None:
    """Cached: pull quality + valuation + cash + dividend metrics for one ticker.
    Single yfinance call covers all four downstream sources."""
    cache_key = ticker.upper()
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    result = await asyncio.to_thread(_fetch_company_metrics_sync, ticker)
    _cache_put(cache_key, result)
    return result


async def get_company_metrics_batch(tickers: list[str]) -> dict[str, CompanyMetrics | None]:
    if not tickers:
        return {}
    tasks = [get_company_metrics(t) for t in tickers]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, CompanyMetrics | None] = {}
    for t, res in zip(tickers, results, strict=True):
        if isinstance(res, BaseException):
            logger.debug("fundamentals: batch metrics error for %s: %s", t, res)
            out[t.upper()] = None
        else:
            out[t.upper()] = res
    return out
