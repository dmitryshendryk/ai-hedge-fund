"""Detector: price trading above the mean analyst target.

The buildable proxy for a "chased upgrade" — when price already exceeds the
consensus target, further upside is limited and recent upgrades may reflect
momentum rather than fundamental headroom. No NLP (report text is paywalled);
uses the numeric target only. Overlay-only: does NOT alter the Discovery score.
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.fundamentals_service import get_company_metrics_batch
from app.backend.services.pricing_service import get_technical_snapshot

logger = logging.getLogger(__name__)

_STRETCH_MULT = 1.15  # price >= 1.15x target -> WARNING


async def detect_exhausted_analyst(ticker: str) -> list[RedFlagFinding]:
    """At most one finding. Empty on missing target, missing price, or price
    below target.
    """
    sym = ticker.strip().upper()
    snap = await get_technical_snapshot(sym)
    if snap is None:
        return []
    price = snap.latest_close

    metrics_by_ticker = await get_company_metrics_batch([sym])
    m = metrics_by_ticker.get(sym)
    target = getattr(m, "target_mean_price", None) if m is not None else None
    if target is None or target <= 0:
        return []
    if price < target:
        return []

    if price >= _STRETCH_MULT * target:
        severity, score = Severity.WARNING, 40.0
    else:
        severity, score = Severity.INFO, 20.0

    headline = (
        f"Above analyst target: trading at ${price:.2f} vs ${target:.2f} mean target "
        "- limited upside / chased momentum"
    )
    return [RedFlagFinding(
        detector="exhausted_analyst",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "latest_close": price,
            "target_mean_price": target,
            "price_to_target": round(price / target, 3),
        },
    )]
