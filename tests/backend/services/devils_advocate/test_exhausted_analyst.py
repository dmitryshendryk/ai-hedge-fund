from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

import app.backend.services.devils_advocate_service._exhausted_analyst as ea
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.pricing_service import TechnicalSnapshot


@dataclass
class _FakeMetrics:
    target_mean_price: float | None


def _snap(close):
    return TechnicalSnapshot("TEST", close, 100.0, 0.0, 50.0)


def _batch_mock(target):
    return AsyncMock(return_value={"TEST": _FakeMetrics(target_mean_price=target)})


@pytest.mark.asyncio
async def test_warning_far_above_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(130.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out[0].severity == Severity.WARNING
    assert out[0].score == 40.0


@pytest.mark.asyncio
async def test_info_just_above_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(105.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out[0].severity == Severity.INFO
    assert out[0].score == 20.0


@pytest.mark.asyncio
async def test_no_finding_below_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(80.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_finding_missing_target():
    with patch.object(ea, "get_technical_snapshot", return_value=_snap(130.0)), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(None)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []


@pytest.mark.asyncio
async def test_no_finding_missing_snapshot():
    with patch.object(ea, "get_technical_snapshot", return_value=None), \
         patch.object(ea, "get_company_metrics_batch", _batch_mock(100.0)):
        out = await ea.detect_exhausted_analyst("TEST")
    assert out == []
