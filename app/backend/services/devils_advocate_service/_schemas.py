"""Pydantic schemas + Severity enum for the Devil's Advocate overlay.

This service NEVER mutates Discovery scores. It surfaces a parallel
"Red Flag Score" (0-100, higher = more reasons to avoid) that the UI
shows as a non-intrusive badge next to the bullish score.
"""
from enum import Enum

from pydantic import BaseModel


class Severity(str, Enum):
    NONE = "none"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class RedFlagFinding(BaseModel):
    """One bear-thesis observation from a single detector.

    score: 0-100 contribution to the per-ticker Red Flag Score. Detectors
    pick from a small palette of tiered values (e.g. 30/45/60) so the UI
    can compare findings across detectors without surprise.
    """
    detector: str
    score: float
    severity: Severity
    headline: str
    detail: dict


class RedFlagReport(BaseModel):
    """Per-ticker Devil's Advocate verdict. `disabled=True` means the user
    has the feature toggle off; in that case findings is always empty and
    score is 0 — the UI uses that to render nothing rather than 'no flags'.
    """
    ticker: str
    score: float
    severity: Severity
    findings: list[RedFlagFinding]
    disabled: bool
