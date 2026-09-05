"""whale_exit alert rule.

Fires when a tracked whale reduces a position by ≥25% QoQ in their latest
13F vs their prior 13F, AND the affected ticker is on the user's watchlist.

Compares the two most recent period_end columns from edgartools' holding_history
DataFrame per whale (only one filing fetched per whale per rule scan — the
heavy lifting is the EDGAR fetch, which is cached for an hour by edgartools).

Severity:
  - warning: 25-49% reduction
  - critical: ≥50% reduction OR full exit (post-share == 0)
"""

import asyncio
import logging

from app.backend.database import SessionLocal
from app.backend.database.models import WhaleFund
from app.backend.services.alert_service._rules._helpers import watchlist_ticker_set
from app.backend.services.alert_service._types import AlertCandidate

logger = logging.getLogger(__name__)

_REDUCTION_MIN = 0.25
_REDUCTION_CRITICAL = 0.50


def _whales() -> list[tuple[int, str]]:
    db = SessionLocal()
    try:
        return [(w.cik, w.name) for w in db.query(WhaleFund).all()]
    finally:
        db.close()


def _scan_whale_sync(whale_cik: int) -> list[dict]:
    """Returns [{ticker, prev_shares, cur_shares, reduction_pct}] for every
    position the whale cut by >= the minimum threshold.
    """
    from app.backend.services.whale_entry_service import (
        _get_holding_history_sync,
        _get_whale_latest_filing_sync,
    )

    accession = _get_whale_latest_filing_sync(whale_cik)
    if accession is None:
        return []
    history = _get_holding_history_sync(accession, 2)
    if history is None or not history.periods or len(history.periods) < 2:
        return []

    periods_sorted = sorted(history.periods)
    prev_p, cur_p = periods_sorted[-2], periods_sorted[-1]

    out: list[dict] = []
    for record in history.records:
        if not record.ticker:
            continue
        prev = record.periods_data.get(prev_p)
        cur = record.periods_data.get(cur_p)
        if prev is None or prev <= 0:
            continue
        cur_v = cur or 0
        if cur_v >= prev:
            continue
        reduction = (prev - cur_v) / prev
        if reduction < _REDUCTION_MIN:
            continue
        out.append({
            "ticker": record.ticker.upper(),
            "prev_shares": int(prev),
            "cur_shares": int(cur_v),
            "reduction_pct": reduction,
        })
    return out


async def evaluate(_thresholds: dict) -> list[AlertCandidate]:
    watchlist = watchlist_ticker_set()
    if not watchlist:
        return []
    whales = _whales()
    if not whales:
        return []

    scans = await asyncio.gather(
        *(asyncio.to_thread(_scan_whale_sync, cik) for cik, _ in whales),
        return_exceptions=True,
    )

    out: list[AlertCandidate] = []
    for (cik, name), scan in zip(whales, scans, strict=True):
        if isinstance(scan, BaseException):
            logger.warning("whale_exit: scan failed for CIK %d: %s", cik, scan)
            continue
        for entry in scan:
            ticker = entry["ticker"]
            if ticker not in watchlist:
                continue

            reduction = entry["reduction_pct"]
            reduction_pct = reduction * 100
            full_exit = entry["cur_shares"] == 0
            is_critical = reduction >= _REDUCTION_CRITICAL or full_exit
            severity = "critical" if is_critical else "warning"

            action = (
                "fully exited" if full_exit
                else f"cut {reduction_pct:.0f}% of their stake in"
            )
            out.append(AlertCandidate(
                rule_type="whale_exit",
                ticker=ticker[:20],
                title=f"🚪 Whale exit: {name[:30]} {action} {ticker}",
                message=(
                    f"{name} reduced their {ticker} position in the latest 13F.\n"
                    f"Prior quarter: {entry['prev_shares']:,} shares\n"
                    f"Latest quarter: {entry['cur_shares']:,} shares\n"
                    f"Reduction: -{reduction_pct:.0f}%\n"
                    "When the whale who validated the thesis walks, reconsider the thesis."
                ),
                payload={
                    "ticker": ticker,
                    "whale_cik": cik,
                    "whale_name": name,
                    "prev_shares": entry["prev_shares"],
                    "cur_shares": entry["cur_shares"],
                    "reduction_pct": reduction,
                    "full_exit": full_exit,
                },
                severity=severity,
            ))
    return out
