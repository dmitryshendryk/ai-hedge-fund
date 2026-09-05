from unittest.mock import patch

import pytest

import app.backend.services.devils_advocate_service._technical_exhaustion as te
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.pricing_service import TechnicalSnapshot


def _snap(pct_above, rsi):
    return TechnicalSnapshot(
        ticker="TEST", latest_close=150.0, sma200=100.0,
        pct_above_sma=pct_above, rsi14=rsi,
    )


@pytest.mark.asyncio
async def test_critical_when_stretched_and_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(35.0, 82.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert len(out) == 1
    assert out[0].severity == Severity.CRITICAL
    assert out[0].score == 60.0
    assert out[0].detector == "technical_exhaustion"


@pytest.mark.asyncio
async def test_warning_when_only_stretched():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(35.0, 60.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.WARNING
    assert out[0].score == 40.0


@pytest.mark.asyncio
async def test_warning_when_only_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(10.0, 80.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.WARNING


@pytest.mark.asyncio
async def test_info_band():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(25.0, 60.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out[0].severity == Severity.INFO
    assert out[0].score == 20.0


@pytest.mark.asyncio
async def test_no_finding_when_calm():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(5.0, 55.0)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_snapshot_returns_empty():
    with patch.object(te, "get_technical_snapshot", return_value=None):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_rsi_none_treated_as_not_overbought():
    with patch.object(te, "get_technical_snapshot", return_value=_snap(10.0, None)):
        out = await te.detect_technical_exhaustion("TEST")
    assert out == []  # ext below 20 and no RSI signal
