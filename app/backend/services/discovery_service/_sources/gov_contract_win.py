"""Discovery source: federal prime contract wins (the 'revenue fuel' signal).

A signed $50M Department of Defense award today translates to recognized
revenue 3-6 months from now. By the time that revenue shows in the 10-Q,
the equity has typically moved. This source pulls recent (30d) federal
awards via USASpending.gov and emits a signal when a watchlist or
Discovery-universe ticker has ≥$10M in fresh prime contracts.

Universe = watchlist + top 30 from recent DiscoverySnapshot batch. Bounded
because we need a company-name lookup per ticker (yfinance .info), and the
USASpending API isn't built for high-frequency polling.

Score:
  - +15: $10M-$25M total in last 30d (qualifying win)
  - +25: $25M-$100M (significant)
  - +40: ≥$100M (major program win — the SNDK-style "this is real" signal)
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal

logger = logging.getLogger(__name__)

_QUALIFYING_MIN = 10_000_000.0
_SIGNIFICANT_MIN = 25_000_000.0
_MAJOR_MIN = 100_000_000.0

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


async def fetch() -> list[tuple[str, IdeaSignal]]:
    from app.backend.services.fundamentals_service import get_company_metrics_batch
    from app.backend.services.gov_contract_service import get_recent_awards_batch

    universe = _gather_universe()
    if not universe:
        return []

    metrics_by_ticker = await get_company_metrics_batch(universe)

    # Without long_name we can't query USASpending (their search is by
    # recipient name, not ticker). Tickers yfinance hasn't tagged drop out.
    name_to_ticker: dict[str, str] = {}
    for ticker, m in metrics_by_ticker.items():
        if m is None or not m.long_name:
            continue
        name_to_ticker[m.long_name] = ticker

    if not name_to_ticker:
        return []

    awards_by_name = await get_recent_awards_batch(list(name_to_ticker.keys()))

    out: list[tuple[str, IdeaSignal]] = []
    for name, ticker in name_to_ticker.items():
        summary = awards_by_name.get(name)
        if summary is None or summary.total_value < _QUALIFYING_MIN:
            continue

        if summary.total_value >= _MAJOR_MIN:
            score = 40.0
            tier_label = "major program win"
        elif summary.total_value >= _SIGNIFICANT_MIN:
            score = 25.0
            tier_label = "significant award"
        else:
            score = 15.0
            tier_label = "qualifying win"

        total_m = summary.total_value / 1e6
        label = f"{tier_label}: ${total_m:,.0f}M federal awards (30d)"

        out.append((ticker, IdeaSignal(
            source="gov_contract_win",
            score=score,
            label=label,
            detail={
                "ticker": ticker,
                "company": name,
                "total_value": summary.total_value,
                "award_count": summary.award_count,
                "latest_award_date": summary.latest_award_date,
                "latest_description": summary.latest_description,
                "tier": tier_label,
            },
        )))
    return out
