from unittest.mock import patch

import pandas as pd
import pytest

import app.backend.services.pricing_service as ps
from app.backend.services.pricing_service import _compute_rsi


def test_rsi_all_gains_approaches_100():
    # Monotonically rising series -> RSI near 100 (no losses).
    closes = pd.Series([float(x) for x in range(1, 30)])
    rsi = _compute_rsi(closes, period=14)
    assert rsi is not None
    assert rsi > 99.0


def test_rsi_insufficient_data_returns_none():
    closes = pd.Series([1.0, 2.0, 3.0])
    assert _compute_rsi(closes, period=14) is None


def test_rsi_known_series_midrange():
    # Alternating up/down of equal size -> RSI hovers near 50.
    vals = []
    price = 100.0
    for i in range(40):
        price += 1.0 if i % 2 == 0 else -1.0
        vals.append(price)
    rsi = _compute_rsi(pd.Series(vals), period=14)
    assert rsi is not None
    assert 30.0 < rsi < 70.0


class _FakeTicker:
    def __init__(self, frame):
        self._frame = frame

    def history(self, start=None, end=None, auto_adjust=True):
        return self._frame


def _rising_frame(n=260):
    idx = pd.date_range("2025-01-01", periods=n, freq="D")
    closes = [100.0 + i for i in range(n)]  # steady uptrend
    return pd.DataFrame({"Close": closes}, index=idx)


@pytest.mark.asyncio
async def test_technical_snapshot_uptrend():
    frame = _rising_frame()
    with patch.object(ps.yf, "Ticker", return_value=_FakeTicker(frame)):
        snap = await ps.get_technical_snapshot("TEST", sma_days=200)
    assert snap is not None
    assert snap.ticker == "TEST"
    assert snap.latest_close > snap.sma200          # price above its own SMA
    assert snap.pct_above_sma > 0
    assert snap.rsi14 is not None and snap.rsi14 > 90.0  # relentless uptrend


@pytest.mark.asyncio
async def test_technical_snapshot_short_history_returns_none():
    short = pd.DataFrame(
        {"Close": [100.0, 101.0, 102.0]},
        index=pd.date_range("2025-01-01", periods=3, freq="D"),
    )
    with patch.object(ps.yf, "Ticker", return_value=_FakeTicker(short)):
        snap = await ps.get_technical_snapshot("TEST", sma_days=200)
    assert snap is None
