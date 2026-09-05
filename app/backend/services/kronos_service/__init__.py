"""Reads Kronos forecast artifacts written out of process.

Imports no torch, so the API process never depends on PyTorch. A forecast past
_MAX_AGE_HOURS is refused rather than served weak.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ARTIFACT = ".out/kronos_forecasts.json"
_ENV_VAR = "KRONOS_FORECAST_PATH"
_SCHEMA_VERSION = 1

# A 5-day forecast written yesterday has already spent a fifth of its horizon.
_MAX_AGE_HOURS: float = 24.0
_CACHE_TTL_SECONDS: float = 300.0


class KronosStatus(StrEnum):
    """Why forecasts are or are not usable, for diagnostics."""

    OK = "ok"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    SCHEMA_MISMATCH = "schema_mismatch"
    STALE = "stale"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True, kw_only=True)
class KronosHorizon:
    """Sampled-path statistics for one forecast horizon.

    prob_up is a Monte Carlo estimate whose standard error is about
    sqrt(0.25 / sample_count), so a threshold on it is a band, not a point.
    """

    days: int
    prob_up: float  # share of sampled paths closing above the last actual close
    expected_return_pct: float
    p10_return_pct: float
    p90_return_pct: float


@dataclass(frozen=True, slots=True, kw_only=True)
class KronosForecast:
    """One ticker's forecast as read from the artifact."""

    ticker: str
    last_close: float
    generated_at: str
    model: str
    sample_count: int
    horizons: tuple[KronosHorizon, ...] = field(default_factory=tuple)

    def horizon(self, days: int) -> KronosHorizon | None:
        """The horizon matching `days`, or None when the worker did not emit it."""
        for entry in self.horizons:
            if entry.days == days:
                return entry
        return None


@dataclass(frozen=True, slots=True, kw_only=True)
class _Load:
    """Outcome of one artifact read, cached against the file's mtime."""

    status: KronosStatus
    forecasts: dict[str, KronosForecast] = field(default_factory=dict)
    mtime: float = 0.0
    read_at: float = 0.0


_cached: _Load | None = None


def artifact_path() -> Path:
    """Where the worker writes and this reader looks."""
    return Path(os.environ.get(_ENV_VAR) or _DEFAULT_ARTIFACT)


def _horizons_from(raw: object) -> tuple[KronosHorizon, ...]:
    if not isinstance(raw, dict):
        return ()
    out: list[KronosHorizon] = []
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        try:
            out.append(KronosHorizon(
                days=int(key),
                prob_up=float(value["prob_up"]),
                expected_return_pct=float(value["expected_return_pct"]),
                p10_return_pct=float(value["p10_return_pct"]),
                p90_return_pct=float(value["p90_return_pct"]),
            ))
        except (TypeError, ValueError, KeyError):
            continue
    return tuple(sorted(out, key=lambda h: h.days))


def _forecasts_from(payload: dict, generated_at: str, model: str, sample_count: int) -> dict[str, KronosForecast]:
    out: dict[str, KronosForecast] = {}
    for ticker, entry in (payload.get("forecasts") or {}).items():
        if not isinstance(entry, dict):
            continue
        try:
            last_close = float(entry["last_close"])
        except (TypeError, ValueError, KeyError):
            continue
        horizons = _horizons_from(entry.get("horizons"))
        if last_close <= 0 or not horizons:
            continue
        sym = str(ticker).strip().upper()
        out[sym] = KronosForecast(
            ticker=sym,
            last_close=last_close,
            generated_at=generated_at,
            model=model,
            sample_count=sample_count,
            horizons=horizons,
        )
    return out


def _parse(payload: dict, mtime: float) -> _Load:
    """Validate the envelope, then the per-ticker entries."""
    if int(payload.get("schema_version") or 0) != _SCHEMA_VERSION:
        logger.warning("kronos: artifact schema_version is not %d", _SCHEMA_VERSION)
        return _Load(status=KronosStatus.SCHEMA_MISMATCH, mtime=mtime, read_at=time.monotonic())

    try:
        generated_at = datetime.fromisoformat(str(payload.get("generated_at")))
    except (TypeError, ValueError):
        logger.warning("kronos: artifact generated_at is not an ISO timestamp")
        return _Load(status=KronosStatus.UNREADABLE, mtime=mtime, read_at=time.monotonic())
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - generated_at).total_seconds() / 3600.0
    if age_hours > _MAX_AGE_HOURS:
        logger.info("kronos: artifact is %.1fh old, limit %.0fh", age_hours, _MAX_AGE_HOURS)
        return _Load(status=KronosStatus.STALE, mtime=mtime, read_at=time.monotonic())

    params = payload.get("params") or {}
    try:
        sample_count = int(params.get("sample_count") or 0)
    except (TypeError, ValueError):
        sample_count = 0

    forecasts = _forecasts_from(
        payload,
        generated_at=generated_at.isoformat(),
        model=str(payload.get("model") or "unknown"),
        sample_count=sample_count,
    )
    return _Load(
        status=KronosStatus.OK if forecasts else KronosStatus.EMPTY,
        forecasts=forecasts,
        mtime=mtime,
        read_at=time.monotonic(),
    )


def _load() -> _Load:
    """Cached read. A rewritten artifact is picked up on mtime change, not on TTL."""
    global _cached

    path = artifact_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cached = None
        return _Load(status=KronosStatus.MISSING)

    if _cached is not None and _cached.mtime == mtime and time.monotonic() - _cached.read_at <= _CACHE_TTL_SECONDS:
        return _cached

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        logger.warning("kronos: cannot read %s: %s", path, exc)
        result = _Load(status=KronosStatus.UNREADABLE, mtime=mtime, read_at=time.monotonic())
    else:
        result = (
            _parse(payload, mtime)
            if isinstance(payload, dict)
            else _Load(status=KronosStatus.UNREADABLE, mtime=mtime, read_at=time.monotonic())
        )

    _cached = result
    return result


def get_forecast(ticker: str) -> KronosForecast | None:
    """One ticker's forecast, or None when absent, stale or unreadable."""
    return _load().forecasts.get(ticker.strip().upper())


def get_all_forecasts() -> dict[str, KronosForecast]:
    """Every usable forecast. Empty when the artifact cannot be served."""
    return dict(_load().forecasts)


def get_status() -> KronosStatus:
    """Whether forecasts are usable, and if not, why."""
    return _load().status


def clear_cache() -> int:
    """Drop the parsed artifact. Returns 1 when something was cached."""
    global _cached
    had = _cached is not None
    _cached = None
    return 1 if had else 0
