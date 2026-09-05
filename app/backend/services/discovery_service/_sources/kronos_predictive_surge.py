"""Discovery source: Kronos foundation-model forecast of a near-term advance.

Reads forecasts the out-of-process worker already wrote, so the fanout costs a
file read. The universe is whatever the worker forecast.

Emits only when the sampled paths agree on direction AND on a positive mean:
a high prob_up beside a negative expected return is negative skew, many small
gains against a few large losses, which is a setup to avoid rather than buy.
"""

import logging

from app.backend.models.discovery_schemas import IdeaSignal
from app.backend.services.kronos_service import KronosForecast, get_all_forecasts

logger = logging.getLogger(__name__)

_HORIZON_DAYS = 5
_MIN_PROB_UP = 0.70
_SURGE_SCORE = 25.0

# Below this, prob_up's standard error exceeds ~4 points and a threshold on it
# reads sampling noise as conviction.
_MIN_SAMPLE_COUNT = 32


def _signal_for(forecast: KronosForecast) -> IdeaSignal | None:
    horizon = forecast.horizon(_HORIZON_DAYS)
    if horizon is None:
        return None
    if horizon.prob_up < _MIN_PROB_UP or horizon.expected_return_pct <= 0:
        return None

    return IdeaSignal(
        source="kronos_predictive_surge",
        score=_SURGE_SCORE,
        label=(
            f"Kronos: {horizon.prob_up * 100:.0f}% of paths higher in {_HORIZON_DAYS}d, "
            f"{horizon.expected_return_pct:+.1f}% expected"
        ),
        detail={
            "ticker": forecast.ticker,
            "horizon_days": _HORIZON_DAYS,
            "prob_up": horizon.prob_up,
            "expected_return_pct": horizon.expected_return_pct,
            "p10_return_pct": horizon.p10_return_pct,
            "p90_return_pct": horizon.p90_return_pct,
            "last_close": forecast.last_close,
            "model": forecast.model,
            "sample_count": forecast.sample_count,
            "generated_at": forecast.generated_at,
        },
    )


async def fetch() -> list[tuple[str, IdeaSignal]]:
    try:
        forecasts = get_all_forecasts()
        if not forecasts:
            return []

        out: list[tuple[str, IdeaSignal]] = []
        for ticker, forecast in forecasts.items():
            if forecast.sample_count < _MIN_SAMPLE_COUNT:
                logger.info(
                    "kronos_predictive_surge: %s forecast used %d samples, need %d",
                    ticker, forecast.sample_count, _MIN_SAMPLE_COUNT,
                )
                continue
            signal = _signal_for(forecast)
            if signal is not None:
                out.append((ticker, signal))
        return out
    except Exception as exc:
        # The engine gathers sources with return_exceptions=True, so a raise
        # would only be logged as a dead source. Degrade to no ideas instead.
        logger.warning("kronos_predictive_surge: source failed: %s", exc)
        return []
