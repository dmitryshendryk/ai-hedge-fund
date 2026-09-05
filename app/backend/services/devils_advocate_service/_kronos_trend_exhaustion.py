"""Detector: Kronos forecasts a downtrend (mean-reversion risk).

Overlay-only, like every detector here: it never alters the Discovery score.

Reported at INFO and scored low on purpose. Measured prob_up clusters near 0 or
1 rather than spreading like a probability, and the forecast sign flips with the
lookback window, so this reads as an unvalidated opinion. tools/kronos_backtest.py
is what would justify raising it. Until then it must not push a report to
CRITICAL, which get_red_flags does by summing finding scores.
"""

import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.kronos_service import get_forecast

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 7

# Read on the downside: prob_down = 1 - prob_up.
_PROB_DOWN_WARNING = 0.65

# A confident drift of a fraction of a percent is not worth a badge.
_MIN_DROP_PCT = 2.0

_MIN_SAMPLE_COUNT = 32

# Well below the 60 that makes a report CRITICAL and the 30 that makes it WARNING.
_INFO_SCORE = 20.0


async def detect_kronos_trend_exhaustion(ticker: str) -> list[RedFlagFinding]:
    """At most one INFO finding. Empty when no fresh forecast covers the horizon."""
    sym = ticker.strip().upper()
    try:
        forecast = get_forecast(sym)
        if forecast is None or forecast.sample_count < _MIN_SAMPLE_COUNT:
            return []

        horizon = forecast.horizon(_HORIZON_DAYS)
        if horizon is None:
            return []

        prob_down = 1.0 - horizon.prob_up
        expected = horizon.expected_return_pct
        if prob_down < _PROB_DOWN_WARNING or expected > -_MIN_DROP_PCT:
            return []

        return [RedFlagFinding(
            detector="kronos_trend_exhaustion",
            score=_INFO_SCORE,
            severity=Severity.INFO,
            headline=(
                f"Kronos forecasts a decline over {_HORIZON_DAYS}d "
                f"({expected:+.1f}% expected, unvalidated model)"
            ),
            detail={
                "ticker": sym,
                "horizon_days": _HORIZON_DAYS,
                "prob_down": round(prob_down, 4),
                "expected_return_pct": expected,
                "p10_return_pct": horizon.p10_return_pct,
                "p90_return_pct": horizon.p90_return_pct,
                "last_close": forecast.last_close,
                "model": forecast.model,
                "sample_count": forecast.sample_count,
                "generated_at": forecast.generated_at,
                "calibrated": False,
            },
        )]
    except Exception as exc:
        logger.debug("kronos_trend_exhaustion: detection failed for %s: %s", sym, exc)
        return []
