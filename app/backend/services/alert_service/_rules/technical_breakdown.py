"""technical_breakdown exit-alert rule.

Fires when a watchlist ticker's latest close drops below its 200-day SMA —
the classic "stage 2 → stage 4 transition" technical breakdown that
precedes most multi-month declines. Bounded by the watchlist size; one
yfinance fetch per held name per rule scan.

Severity:
  - warning: close 0-10% below 200-day SMA
  - critical: close >10% below 200-day SMA (deep breakdown)
"""

import asyncio
import logging

from app.backend.services.alert_service._rules._helpers import watchlist_ticker_set
from app.backend.services.alert_service._types import AlertCandidate

logger = logging.getLogger(__name__)

_SMA_DAYS = 200
_CRITICAL_BREAKDOWN_PCT = -10.0


async def evaluate(_thresholds: dict) -> list[AlertCandidate]:
    from app.backend.services.pricing_service import get_sma_cross

    watchlist = sorted(watchlist_ticker_set())
    if not watchlist:
        return []

    results = await asyncio.gather(
        *(get_sma_cross(t, _SMA_DAYS) for t in watchlist),
        return_exceptions=True,
    )

    out: list[AlertCandidate] = []
    for ticker, res in zip(watchlist, results, strict=True):
        if isinstance(res, BaseException) or res is None:
            continue
        if res.pct_above_sma >= 0:
            continue

        is_critical = res.pct_above_sma <= _CRITICAL_BREAKDOWN_PCT
        severity = "critical" if is_critical else "warning"
        gap_pct = abs(res.pct_above_sma)

        out.append(AlertCandidate(
            rule_type="technical_breakdown",
            ticker=ticker[:20],
            title=f"🚪 Technical breakdown: {ticker} closed {gap_pct:.1f}% below 200d SMA",
            message=(
                f"{ticker} broke below its 200-day SMA.\n"
                f"Latest close: ${res.latest_close:.2f}\n"
                f"200d SMA: ${res.sma:.2f}\n"
                f"Gap: -{gap_pct:.1f}%\n"
                "Trend likely broken — consider reducing exposure or tightening stops."
            ),
            payload={
                "ticker": ticker,
                "latest_close": res.latest_close,
                "sma_200d": res.sma,
                "pct_below_sma": res.pct_above_sma,
            },
            severity=severity,
        ))
    return out
