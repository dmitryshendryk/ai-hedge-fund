"""Macro Regime detector — gates every Discovery signal by market weather.

Pulls three FRED series and classifies the tape:
  - T10Y2Y       : 10y - 2y Treasury spread. Negative = inverted curve.
  - VIXCLS       : CBOE Volatility Index close.
  - BAMLH0A0HYM2 : ICE BofA US High Yield Option-Adjusted Spread (percent).

Risk-Off when ANY of:
  - Inverted curve (T10Y2Y < 0) AND VIX > 25
  - VIX > 30 (panic)
  - HY OAS > 5.0 (credit stress)

Risk-Off applies a 0.3x multiplier to every Discovery score. Soft gate by
design — we keep ranking information when buyer signals fire during a
crash; they just don't dominate cheap-bond / yield-curve evidence.

FRED_API_KEY required in env. Without it, get_regime() returns a neutral
(risk_on, 1.0) regime so the engine degrades gracefully.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Literal

import requests

logger = logging.getLogger(__name__)

_FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
_CACHE_TTL_SECONDS: float = 3600.0  # regime doesn't flip hourly

_VIX_PANIC = 30.0
_VIX_ELEVATED = 25.0
_HY_OAS_STRESS = 5.0
_RISK_OFF_MULTIPLIER = 0.3

_SERIES = {
    "T10Y2Y": "yield_curve_10y_2y",
    "VIXCLS": "vix",
    "BAMLH0A0HYM2": "hy_oas",
}

RegimeMode = Literal["risk_on", "risk_off"]


@dataclass(frozen=True)
class FredObservation:
    value: float | None
    as_of: str | None


@dataclass(frozen=True)
class RegimeClassification:
    mode: RegimeMode
    reasons: list[str]


@dataclass(frozen=True)
class MacroRegime:
    """Result of a regime classification snapshot.

    score_multiplier: applied to every aggregated idea score by the engine.
    reasons: empty in risk_on; populated strings explain the discount.
    metrics: current FRED series values surfaced in the UI banner.
    """
    mode: RegimeMode
    score_multiplier: float
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, float | None] = field(default_factory=dict)
    as_of: str | None = None


_cache: tuple[MacroRegime, float] | None = None
_inflight: asyncio.Task | None = None


def _fetch_latest_observation_sync(series_id: str, api_key: str) -> FredObservation:
    """FRED returns '.' for missing observations — walk back from tail until
    a real value appears. Network/parse errors degrade to an empty observation
    so a single flaky series can't blow up the regime call.
    """
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 10,
    }
    try:
        resp = requests.get(_FRED_BASE, params=params, timeout=10)
        resp.raise_for_status()
        observations = resp.json().get("observations") or []
    except Exception as exc:
        logger.warning("macro: FRED fetch failed for %s: %s", series_id, exc)
        return FredObservation(value=None, as_of=None)

    for obs in observations:
        raw = obs.get("value", ".")
        if raw in (".", "", None):
            continue
        try:
            return FredObservation(value=float(raw), as_of=obs.get("date"))
        except (TypeError, ValueError):
            continue
    return FredObservation(value=None, as_of=None)


def _classify(metrics: dict[str, float | None]) -> RegimeClassification:
    """Apply risk-off rules. Order of reasons preserved for the UI banner."""
    reasons: list[str] = []
    vix = metrics.get("vix")
    curve = metrics.get("yield_curve_10y_2y")
    hy_oas = metrics.get("hy_oas")

    if vix is not None and vix > _VIX_PANIC:
        reasons.append(f"VIX panic ({vix:.1f} > {_VIX_PANIC:.0f})")
    if curve is not None and curve < 0 and vix is not None and vix > _VIX_ELEVATED:
        reasons.append(f"inverted curve + elevated VIX ({curve:+.2f}%, VIX {vix:.1f})")
    if hy_oas is not None and hy_oas > _HY_OAS_STRESS:
        reasons.append(f"credit stress (HY OAS {hy_oas:.2f}% > {_HY_OAS_STRESS:.1f}%)")

    mode: RegimeMode = "risk_off" if reasons else "risk_on"
    return RegimeClassification(mode=mode, reasons=reasons)


def _compute_regime_sync() -> MacroRegime:
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        logger.info("macro: FRED_API_KEY missing — defaulting to risk_on")
        return MacroRegime(mode="risk_on", score_multiplier=1.0, reasons=[], metrics={}, as_of=None)

    metrics: dict[str, float | None] = {}
    latest_as_of: str | None = None
    for series_id, friendly_key in _SERIES.items():
        obs = _fetch_latest_observation_sync(series_id, api_key)
        metrics[friendly_key] = obs.value
        if obs.as_of and (latest_as_of is None or obs.as_of > latest_as_of):
            latest_as_of = obs.as_of

    classification = _classify(metrics)
    multiplier = _RISK_OFF_MULTIPLIER if classification.mode == "risk_off" else 1.0
    return MacroRegime(
        mode=classification.mode,
        score_multiplier=multiplier,
        reasons=classification.reasons,
        metrics=metrics,
        as_of=latest_as_of,
    )


async def get_regime() -> MacroRegime:
    """Cached: return the current MacroRegime. 1h TTL + inflight de-dup.

    Concurrent Discovery requests on cold cache share one FRED fetch.
    """
    global _cache, _inflight

    if _cache is not None:
        regime, ts = _cache
        if time.monotonic() - ts <= _CACHE_TTL_SECONDS:
            return regime
        _cache = None

    if _inflight is not None and not _inflight.done():
        return await asyncio.shield(_inflight)

    async def _run() -> MacroRegime:
        global _cache, _inflight
        try:
            regime = await asyncio.to_thread(_compute_regime_sync)
            _cache = (regime, time.monotonic())
            return regime
        finally:
            _inflight = None

    _inflight = asyncio.create_task(_run())
    return await _inflight


def invalidate_cache() -> None:
    """Drop the cached regime — wired into cache_service for the admin flush UI."""
    global _cache
    _cache = None
