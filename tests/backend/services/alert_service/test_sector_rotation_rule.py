from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

import app.backend.services.alert_service._rules.sector_rotation_rule as rot


@dataclass
class _Ret:
    period_return_pct: float


def _thresholds():
    return {"smh_max_pct": -2.0, "iwm_min_pct": 1.0, "lookback_days": 3}


def _compute_mock(mapping):
    async def _compute(ticker, since):
        val = mapping.get(ticker.upper())
        return _Ret(val) if val is not None else None
    return _compute


@pytest.mark.asyncio
async def test_fires_on_rotation():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -2.4, "IWM": 1.8})):
        out = await rot.evaluate(_thresholds())
    assert len(out) == 1
    assert out[0].rule_type == "sector_rotation"
    assert out[0].ticker == "_MARKET_"
    assert out[0].severity in ("warning", "critical")


@pytest.mark.asyncio
async def test_no_fire_when_smh_only_flat():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -0.5, "IWM": 1.8})):
        out = await rot.evaluate(_thresholds())
    assert out == []


@pytest.mark.asyncio
async def test_no_fire_when_iwm_not_up():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -3.0, "IWM": -0.2})):
        out = await rot.evaluate(_thresholds())
    assert out == []


@pytest.mark.asyncio
async def test_no_fire_when_pricing_unavailable():
    with patch.object(rot, "compute_alpha", _compute_mock({})):  # both None
        out = await rot.evaluate(_thresholds())
    assert out == []


@pytest.mark.asyncio
async def test_names_at_risk_holdings_in_message_and_payload():
    with patch.object(rot, "compute_alpha", _compute_mock({"SMH": -2.4, "IWM": 1.8})), \
         patch.object(rot, "_at_risk_holdings", AsyncMock(return_value=["ASML", "MU"])):
        out = await rot.evaluate(_thresholds())
    assert len(out) == 1
    assert "ASML" in out[0].message and "MU" in out[0].message
    assert "EXIT WATCH" in out[0].message
    assert out[0].payload["at_risk_holdings"] == ["ASML", "MU"]

