"""Discovery source: volatility contraction pattern (the VCP breakout setup).

A base that tightens week over week while volume dries up means sellers are
running out before the next leg. The pattern fires BEFORE the breakout, which is
what makes it useful beside relative_strength — that source can only reward a
move that already happened, and rewarding it is how a book ends up buying
extended names.

All three legs must hold together:
  - Trend: latest close > 50-day SMA > 200-day SMA
  - Contraction: each of the last three 5-bar weekly ranges below the previous
  - Volume dry-up: latest volume below 70% of the 50-day average

Universe = watchlist + top-N from the recent DiscoverySnapshot batch, capped to
match the other per-ticker-fetch sources: one OHLCV pull is needed per ticker,
and pricing_service allows two concurrent yfinance calls.

Score: +25 when the setup holds. Flat rather than tiered — the pattern either
qualifies or it does not, so there is no severity to grade.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal

logger = logging.getLogger(__name__)

_SETUP_SCORE = 25.0

_UNIVERSE_LOOKBACK_HOURS = 48
_MAX_UNIVERSE_SIZE = 25


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


def _label(snapshot) -> str:
    contraction = " → ".join(f"{pct:.1f}%" for pct in snapshot.weekly_range_pcts)
    dry_up = (1.0 - snapshot.volume_ratio) * 100.0
    return f"VCP base: weekly range {contraction}, volume {dry_up:.0f}% below 50d avg"


async def fetch() -> list[tuple[str, IdeaSignal]]:
    from app.backend.services.pricing_service import get_vcp_snapshot

    try:
        universe = _gather_universe()
        if not universe:
            return []

        # get_vcp_snapshot already queues on the pricing_service semaphore, so
        # no second gate is needed here.
        results = await asyncio.gather(
            *(get_vcp_snapshot(t) for t in universe), return_exceptions=True,
        )

        out: list[tuple[str, IdeaSignal]] = []
        for ticker, result in zip(universe, results, strict=True):
            if isinstance(result, BaseException):
                logger.debug("vcp_breakout_setup: error for %s: %s", ticker, result)
                continue
            if result is None or not result.is_setup:
                continue

            out.append((ticker, IdeaSignal(
                source="vcp_breakout_setup",
                score=_SETUP_SCORE,
                label=_label(result),
                detail={
                    "ticker": ticker,
                    "latest_close": result.latest_close,
                    "sma50": result.sma50,
                    "sma200": result.sma200,
                    "weekly_range_pcts": result.weekly_range_pcts,
                    "latest_volume": result.latest_volume,
                    "avg_volume50": result.avg_volume50,
                    "volume_ratio": result.volume_ratio,
                },
            )))
        return out
    except Exception as exc:
        # The engine gathers sources with return_exceptions=True, so a raise
        # would only be logged as a dead source. Degrade to no ideas instead.
        logger.warning("vcp_breakout_setup: source failed: %s", exc)
        return []
