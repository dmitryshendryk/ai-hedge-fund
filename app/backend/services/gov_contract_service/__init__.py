"""USASpending.gov client — recent federal prime contract awards by recipient.

Powers the gov_contract_win Discovery source. USASpending is the canonical
free source for federal awards (no API key required, official data, daily
refreshed). Award types A/B/C/D are prime contracts (Definitive Contract,
Purchase Order, Delivery Order, BPA Call) — the kinds that fund real R&D
and manufacturing work. We exclude grants and IDV (Indefinite-Delivery
Vehicle) ceiling-only entries since those don't always translate to dollars.

Cached 24h per recipient name — federal awards roll up overnight on the
USASpending side, no point hitting them hourly.
"""

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
_CACHE_TTL_SECONDS: float = 24 * 3600.0
_CACHE_MAX_SIZE: int = 500
_LOOKBACK_DAYS: int = 30
_MAX_RESULTS_PER_QUERY: int = 25
_HTTP_TIMEOUT_S: float = 15.0

# USASpending's keyword award search is a heavy endpoint. Firing a whole
# Discovery universe (up to ~200 names) at it concurrently overwhelms it and
# every request hits the timeout. Bound in-flight requests — mirrors the
# yfinance semaphore in pricing_service. The 24h cache absorbs the rest.
_MAX_CONCURRENCY: int = 6
_semaphore: asyncio.Semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)


@dataclass
class GovContractsSummary:
    """Aggregate of recent federal prime contract awards for one recipient."""
    company_name: str
    total_value: float
    award_count: int
    latest_award_date: str | None
    latest_description: str | None


_cache: OrderedDict[str, tuple[GovContractsSummary | None, float]] = OrderedDict()


def _cache_get(key: str) -> GovContractsSummary | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: GovContractsSummary | None) -> None:
    _cache[key] = (value, time.monotonic())
    while len(_cache) > _CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _payload(company_name: str) -> dict:
    today = date.today()
    start = today - timedelta(days=_LOOKBACK_DAYS)
    return {
        "filters": {
            "keywords": [company_name],
            "time_period": [
                {"start_date": start.isoformat(), "end_date": today.isoformat()},
            ],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "fields": ["Award Amount", "Description", "Action Date", "Recipient Name"],
        "limit": _MAX_RESULTS_PER_QUERY,
        "page": 1,
    }


def _parse_amount(raw: object) -> float:
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _fetch_sync(company_name: str) -> GovContractsSummary | None:
    """Synchronous USASpending call. Returns None on any transport error
    (404, 500, network); empty summary (total_value=0) on a successful
    response with no recent awards."""
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
            resp = client.post(_API_URL, json=_payload(company_name))
            if resp.status_code != 200:
                logger.warning(
                    "gov_contract_service: HTTP %d for %r", resp.status_code, company_name,
                )
                return None
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("gov_contract_service: fetch failed for %r: %s", company_name, exc)
        return None

    results = data.get("results") or []
    if not results:
        return GovContractsSummary(
            company_name=company_name,
            total_value=0.0,
            award_count=0,
            latest_award_date=None,
            latest_description=None,
        )

    total = sum(_parse_amount(r.get("Award Amount")) for r in results)
    by_date = sorted(
        ((str(r.get("Action Date") or ""), r) for r in results),
        reverse=True,
    )
    latest_date, latest_row = by_date[0]
    return GovContractsSummary(
        company_name=company_name,
        total_value=total,
        award_count=len(results),
        latest_award_date=latest_date or None,
        latest_description=(latest_row.get("Description") or None),
    )


async def get_recent_awards(company_name: str) -> GovContractsSummary | None:
    """Cached: federal prime contract awards for one recipient over the last
    30 days. Returns None when the API was unreachable; returns a summary
    with total_value=0 when the call succeeded but the recipient had no
    qualifying awards."""
    name = (company_name or "").strip()
    if not name:
        return None
    cached = _cache_get(name)
    if cached is not None:
        return cached
    async with _semaphore:
        result = await asyncio.to_thread(_fetch_sync, name)
    _cache_put(name, result)
    return result


async def get_recent_awards_batch(company_names: list[str]) -> dict[str, GovContractsSummary | None]:
    """Concurrent fetch for many recipients. Bounded only by httpx defaults
    + the 24h cache — USASpending hasn't published an explicit rate limit
    but treats heavy traffic politely; keep batches modest (≤30 names)."""
    if not company_names:
        return {}
    tasks = [get_recent_awards(n) for n in company_names]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[str, GovContractsSummary | None] = {}
    for name, res in zip(company_names, results, strict=True):
        if isinstance(res, BaseException):
            logger.debug("gov_contract_service batch error for %r: %s", name, res)
            out[name] = None
        else:
            out[name] = res
    return out
