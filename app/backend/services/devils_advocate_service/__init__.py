"""Devil's Advocate — read-only bear-thesis overlay for Discovery tickers.

Public API:
  - get_red_flags(ticker) -> RedFlagReport
  - is_enabled() / set_enabled(bool)
  - RedFlagReport, RedFlagFinding, Severity

This service NEVER mutates Discovery state. It runs alongside Discovery
and returns a per-ticker Red Flag Score that the UI renders as a
non-intrusive badge. The toggle defaults OFF (opt-in), so today's
Discovery behavior is unchanged for users who haven't switched it on.
"""
import asyncio
import logging
import time
from collections import OrderedDict

from app.backend.services.devils_advocate_service._altman_z_score import (
    detect_altman_z_score,
)
from app.backend.services.devils_advocate_service._beneish_m_score import (
    detect_beneish_m_score,
)
from app.backend.services.devils_advocate_service._ceo_cfo_divergence import (
    detect_ceo_cfo_divergence,
)
from app.backend.services.devils_advocate_service._exhausted_analyst import (
    detect_exhausted_analyst,
)
from app.backend.services.devils_advocate_service._forensic_ratios import (
    detect_dupont_leverage_trap,
    detect_interest_coverage,
    detect_montier_c_score,
    detect_piotroski_distress,
)
from app.backend.services.devils_advocate_service._schemas import (
    RedFlagFinding,
    RedFlagReport,
    Severity,
)
from app.backend.services.devils_advocate_service._settings import is_enabled, set_enabled
from app.backend.services.devils_advocate_service._technical_exhaustion import (
    detect_technical_exhaustion,
)

logger = logging.getLogger(__name__)

_MAX_SCORE = 100.0
_CRITICAL_AT = 60.0
_WARNING_AT = 30.0

_CACHE_TTL_SECONDS = 30 * 60  # 30 minutes — insider filings don't move per minute
_CACHE_MAX_SIZE = 200
_cache: OrderedDict[str, tuple[RedFlagReport, float]] = OrderedDict()


def _cache_get(key: str) -> RedFlagReport | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: RedFlagReport) -> None:
    _cache[key] = (value, time.monotonic())
    while len(_cache) > _CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _clamp(score: float) -> float:
    if score < 0:
        return 0.0
    if score > _MAX_SCORE:
        return _MAX_SCORE
    return round(score, 2)


def _severity_for(score: float) -> Severity:
    if score >= _CRITICAL_AT:
        return Severity.CRITICAL
    if score >= _WARNING_AT:
        return Severity.WARNING
    if score > 0:
        return Severity.INFO
    return Severity.NONE


async def get_red_flags(ticker: str) -> RedFlagReport:
    """Per-ticker Devil's Advocate verdict. Returns a `disabled=True` report
    immediately when the toggle is off so callers can branch cheaply.
    Detector failures degrade individual findings, never the report.
    """
    sym = ticker.strip().upper()

    if not is_enabled():
        return RedFlagReport(
            ticker=sym,
            score=0.0,
            severity=Severity.NONE,
            findings=[],
            disabled=True,
        )

    cached = _cache_get(sym)
    if cached is not None:
        return cached

    detector_results = await asyncio.gather(
        detect_ceo_cfo_divergence(sym),
        detect_altman_z_score(sym),
        detect_beneish_m_score(sym),
        detect_technical_exhaustion(sym),
        detect_exhausted_analyst(sym),
        detect_montier_c_score(sym),
        detect_interest_coverage(sym),
        detect_piotroski_distress(sym),
        detect_dupont_leverage_trap(sym),
        return_exceptions=True,
    )

    findings: list[RedFlagFinding] = []
    for result in detector_results:
        if isinstance(result, BaseException):
            logger.warning("devils_advocate: detector failed for %s: %s", sym, result)
            continue
        findings.extend(result)

    total = _clamp(sum(f.score for f in findings))
    findings.sort(key=lambda f: -f.score)
    report = RedFlagReport(
        ticker=sym,
        score=total,
        severity=_severity_for(total),
        findings=findings,
        disabled=False,
    )
    _cache_put(sym, report)
    return report


# Explicit re-exports for consumers (route module, future UI batch endpoint).
# Using explicit imports above instead of __all__ — consumers should also
# import each name explicitly rather than wildcard-import this package.
_ = (RedFlagFinding, RedFlagReport, Severity, is_enabled, set_enabled)
