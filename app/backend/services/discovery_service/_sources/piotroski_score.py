"""Discovery source: a strengthening Piotroski F-Score (the quality signal).

Nine binary tests of profitability, funding and efficiency, all drawn from filed
statements. 7 and above marks a business improving on nearly every axis the score
measures — the bull side of what the piotroski_distress detector reads from the
bottom of the same scale.

Deterministic arithmetic over the cached ForensicBundle: no model, no forecast,
no API beyond the statements the Devil's Advocate overlay already fetched. Within
that 30-minute cache this source costs no extra request.

The scoring function and the bundle-to-line-item mapping are imported rather than
copied — the row-name candidates yfinance forces would drift if duplicated.

Score: +20 when the F-Score is 7, 8 or 9.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal
from app.backend.services.devils_advocate_service._forensic_ratios import _piotroski_year
from app.backend.services.devils_advocate_service._yfinance_fundamentals import get_forensic_bundle
from app.backend.services.fundamentals_service._advanced import piotroski_score

logger = logging.getLogger(__name__)

_MIN_STRONG_SCORE = 7
_QUALIFYING_SCORE = 20.0

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


async def _score_for(ticker: str) -> int | None:
    """F-Score for one ticker, or None when the statements are too thin.

    Column 0 is the latest fiscal year in a ForensicBundle, column 1 the prior.
    """
    bundle = await get_forensic_bundle(ticker)
    if bundle is None:
        return None
    return piotroski_score(current=_piotroski_year(bundle, 0), prior=_piotroski_year(bundle, 1))


async def fetch() -> list[tuple[str, IdeaSignal]]:
    try:
        universe = _gather_universe()
        if not universe:
            return []

        results = await asyncio.gather(*(_score_for(t) for t in universe), return_exceptions=True)

        out: list[tuple[str, IdeaSignal]] = []
        for ticker, result in zip(universe, results, strict=True):
            if isinstance(result, BaseException):
                logger.debug("piotroski_score: error for %s: %s", ticker, result)
                continue
            if result is None or result < _MIN_STRONG_SCORE:
                continue

            out.append((ticker, IdeaSignal(
                source="piotroski_score",
                score=_QUALIFYING_SCORE,
                label=f"Piotroski F-Score {result}/9 — strengthening fundamentals",
                detail={
                    "ticker": ticker,
                    "f_score": result,
                    "min_strong_score": _MIN_STRONG_SCORE,
                },
            )))
        return out
    except Exception as exc:
        # The engine gathers sources with return_exceptions=True, so a raise
        # would only be logged as a dead source. Degrade to no ideas instead.
        logger.warning("piotroski_score: source failed: %s", exc)
        return []
