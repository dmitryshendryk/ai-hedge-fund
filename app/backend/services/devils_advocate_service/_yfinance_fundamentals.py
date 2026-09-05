"""Shared yfinance fundamentals fetch for forensic-accounting detectors.

Both Altman Z-Score and Beneish M-Score need the same annual statements
(balance sheet, income, cash flow) plus sector. Caching at this layer
means running both detectors hits yfinance ONCE per 30min window per
ticker rather than twice. Per-detector caching would still de-duplicate
inside one report run, but two detectors fanning out concurrently would
race the network without this shared cache.
"""
import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30 * 60
_CACHE_MAX_SIZE = 200


@dataclass(frozen=True)
class ForensicBundle:
    """Annual financial statements + sector for forensic ratio detectors.

    Statements may be empty DataFrames when yfinance returns no data
    (delisted, ADR with thin coverage, etc.). Detectors must check
    .empty before indexing.
    """
    ticker: str
    balance_sheet: pd.DataFrame
    income_statement: pd.DataFrame
    cash_flow: pd.DataFrame
    sector: str | None
    market_cap: float | None


class _Sentinel(Enum):
    MISS = "miss"


_MISS = _Sentinel.MISS

_cache: OrderedDict[str, tuple[ForensicBundle | None, float]] = OrderedDict()


def _cache_get(key: str) -> ForensicBundle | None | _Sentinel:
    entry = _cache.get(key)
    if entry is None:
        return _MISS
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return _MISS
    return value


def _cache_put(key: str, value: ForensicBundle | None) -> None:
    _cache[key] = (value, time.monotonic())
    while len(_cache) > _CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _to_df(value: object) -> pd.DataFrame:
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame()


def _fetch_sync(ticker: str) -> ForensicBundle | None:
    sym = ticker.upper()
    try:
        t = yf.Ticker(sym)
        bs = t.balance_sheet
        is_ = t.financials
        cf = t.cashflow
        info = t.info or {}
    except Exception as exc:
        logger.debug("forensic_fundamentals: yfinance fetch failed for %s: %s", sym, exc)
        return None

    market_cap: float | None = None
    raw_mc = info.get("marketCap")
    if raw_mc is not None:
        try:
            market_cap = float(raw_mc)
        except (TypeError, ValueError):
            market_cap = None

    return ForensicBundle(
        ticker=sym,
        balance_sheet=_to_df(bs),
        income_statement=_to_df(is_),
        cash_flow=_to_df(cf),
        sector=info.get("sector") or None,
        market_cap=market_cap,
    )


async def get_forensic_bundle(ticker: str) -> ForensicBundle | None:
    """Fetch (or return cached) annual statements for a ticker. None on
    yfinance failure — detectors must treat None as 'no data, no flag'.
    """
    sym = ticker.strip().upper()
    cached = _cache_get(sym)
    if cached is not _MISS:
        assert not isinstance(cached, _Sentinel)
        return cached
    bundle = await asyncio.to_thread(_fetch_sync, sym)
    _cache_put(sym, bundle)
    return bundle


def safe_row(df: pd.DataFrame, names: tuple[str, ...] | list[str], col_idx: int) -> float | None:
    """Look up the first matching row name at column index; coerce NaN/None
    to None. Returns None if df is empty, the column doesn't exist, or
    no candidate name matches.
    """
    if df is None or df.empty:
        return None
    if col_idx >= len(df.columns):
        return None
    for name in names:
        if name in df.index:
            try:
                val = df.at[name, df.columns[col_idx]]
            except (KeyError, IndexError):
                continue
            if val is None:
                return None
            try:
                if pd.isna(val):
                    return None
                return float(val)
            except (TypeError, ValueError):
                continue
    return None
