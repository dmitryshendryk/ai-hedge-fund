"""Detector: technical exhaustion (mean-reversion risk).

Flags names stretched far above their 200-day trend and/or overbought on
RSI(14). High bullish conviction + stretched technicals = a common trap
where the thesis is right but the entry timing is late. Overlay-only: does
NOT alter the Discovery score.
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.pricing_service import get_technical_snapshot

logger = logging.getLogger(__name__)

_EXT_STRETCHED = 30.0   # % above 200d SMA
_EXT_INFO = 20.0
_RSI_OVERBOUGHT = 75.0


async def detect_technical_exhaustion(ticker: str) -> list[RedFlagFinding]:
    """At most one finding. Empty on missing snapshot or calm technicals."""
    sym = ticker.strip().upper()
    snap = await get_technical_snapshot(sym)
    if snap is None:
        return []

    ext = snap.pct_above_sma
    rsi = snap.rsi14
    overbought = rsi is not None and rsi > _RSI_OVERBOUGHT
    stretched = ext > _EXT_STRETCHED

    if stretched and overbought:
        severity, score = Severity.CRITICAL, 60.0
    elif stretched or overbought:
        severity, score = Severity.WARNING, 40.0
    elif ext > _EXT_INFO:
        severity, score = Severity.INFO, 20.0
    else:
        return []

    rsi_txt = f"RSI {rsi:.0f}" if rsi is not None else "RSI n/a"
    headline = f"Technical exhaustion: +{ext:.0f}% above 200d trend, {rsi_txt} - mean-reversion risk"

    return [RedFlagFinding(
        detector="technical_exhaustion",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "pct_above_sma200": round(ext, 2),
            "rsi14": rsi,
            "latest_close": snap.latest_close,
            "sma200": round(snap.sma200, 2),
        },
    )]
