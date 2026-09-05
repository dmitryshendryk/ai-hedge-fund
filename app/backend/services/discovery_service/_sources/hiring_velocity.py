"""Discovery source: structural headcount growth (the 'hiring velocity' proxy).

yfinance only exposes the CURRENT fullTimeEmployees value (sourced from the
most recent 10-K). We persist our own snapshots in headcount_snapshots so
we can compute YoY growth. The first ~12 months after rollout there'll be
no historical data → no signals emit; the source self-bootstraps from
there.

Honest limitation: this is annual data refreshed when the 10-K lands, not
real-time hiring velocity. Catches multi-quarter structural growth, NOT
"they posted 40 SDE roles last week" — that needs paid job-board APIs.

Universe = watchlist + top 30 from recent DiscoverySnapshot batch.

Score:
  - +15: 15-30% headcount growth YoY (structural expansion)
  - +25: 30-50% YoY (aggressive build-out)
  - +35: ≥50% YoY (rare; usually pre-product-launch or post-acquisition)
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import asc, func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, HeadcountSnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal

logger = logging.getLogger(__name__)

_QUALIFYING_GROWTH = 0.15
_AGGRESSIVE_GROWTH = 0.30
_EXTREME_GROWTH = 0.50

_PRIOR_YEAR_WINDOW_DAYS = 330  # ~11 months — give ourselves room to hit a snapshot
_PRIOR_YEAR_WINDOW_HARD_MIN_DAYS = 270  # don't compare against anything younger than 9mo

_UNIVERSE_LOOKBACK_HOURS = 48
_MAX_UNIVERSE_SIZE = 200


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


def _persist_and_lookup(ticker_to_current: dict[str, int]) -> dict[str, int]:
    """Persist current headcount rows; return {ticker: prior_year_employee_count}
    for tickers with a snapshot in the 9-12mo window."""
    if not ticker_to_current:
        return {}

    now = datetime.now(timezone.utc)
    prior_window_start = now - timedelta(days=_PRIOR_YEAR_WINDOW_DAYS + 60)
    prior_window_end = now - timedelta(days=_PRIOR_YEAR_WINDOW_HARD_MIN_DAYS)

    db = SessionLocal()
    prior_by_ticker: dict[str, int] = {}
    try:
        for ticker, count in ticker_to_current.items():
            db.add(HeadcountSnapshot(ticker=ticker, employee_count=count))

            row = (
                db.query(HeadcountSnapshot)
                .filter(HeadcountSnapshot.ticker == ticker)
                .filter(HeadcountSnapshot.snapshot_at >= prior_window_start)
                .filter(HeadcountSnapshot.snapshot_at <= prior_window_end)
                .order_by(asc(HeadcountSnapshot.snapshot_at))
                .first()
            )
            if row is not None and row.employee_count > 0:
                prior_by_ticker[ticker] = int(row.employee_count)
        db.commit()
    except Exception as exc:
        logger.warning("hiring_velocity: persistence failed: %s", exc)
        db.rollback()
    finally:
        db.close()
    return prior_by_ticker


async def fetch() -> list[tuple[str, IdeaSignal]]:
    from app.backend.services.fundamentals_service import get_company_metrics_batch

    universe = _gather_universe()
    if not universe:
        return []

    metrics_by_ticker = await get_company_metrics_batch(universe)
    current: dict[str, int] = {}
    for ticker, m in metrics_by_ticker.items():
        if m is None or m.full_time_employees is None or m.full_time_employees <= 0:
            continue
        current[ticker] = int(m.full_time_employees)

    prior = _persist_and_lookup(current)

    out: list[tuple[str, IdeaSignal]] = []
    for ticker, cur in current.items():
        prev = prior.get(ticker)
        if prev is None or prev <= 0:
            continue
        growth = (cur - prev) / prev
        if growth < _QUALIFYING_GROWTH:
            continue

        if growth >= _EXTREME_GROWTH:
            score = 35.0
            tier = "extreme hiring"
        elif growth >= _AGGRESSIVE_GROWTH:
            score = 25.0
            tier = "aggressive build-out"
        else:
            score = 15.0
            tier = "structural expansion"

        growth_pct = growth * 100
        label = f"{tier}: {prev:,} → {cur:,} ({growth_pct:+.0f}% YoY)"

        out.append((ticker, IdeaSignal(
            source="hiring_velocity",
            score=score,
            label=label,
            detail={
                "ticker": ticker,
                "current_employees": cur,
                "prior_year_employees": prev,
                "growth_pct": growth,
                "tier": tier,
            },
        )))
    return out
