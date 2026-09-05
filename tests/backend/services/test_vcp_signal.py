"""Tests for the VCP (volatility contraction pattern) arithmetic.

The pattern only means anything when all three legs hold together, so the tests
pin each leg's boundary separately and then prove that any one failure
suppresses the setup. A leg that quietly passed would emit a buy on an ordinary
chart.
"""

import pandas as pd
import pytest

from app.backend.services.pricing_service import (
    VcpSnapshot,
    _build_vcp_snapshot,
    _weekly_range_pcts,
)


def _week_bars(spread: float, close: float) -> dict[str, list[float]]:
    # Five identical bars, so the week's span equals the spread exactly.
    return {
        "High": [close + spread] * 5,
        "Low": [close] * 5,
        "Close": [close] * 5,
    }


def _weeks_frame(spreads: list[float], close: float = 100.0) -> pd.DataFrame:
    rows: dict[str, list[float]] = {"High": [], "Low": [], "Close": []}
    for spread in spreads:
        for key, values in _week_bars(spread, close).items():
            rows[key].extend(values)
    return pd.DataFrame(rows)


def _snapshot(
    ranges: list[float],
    latest_close: float = 150.0,
    sma50: float = 130.0,
    sma200: float = 100.0,
    volume_ratio: float = 0.5,
) -> VcpSnapshot:
    return VcpSnapshot(
        ticker="TEST",
        latest_close=latest_close,
        sma50=sma50,
        sma200=sma200,
        weekly_range_pcts=ranges,
        latest_volume=500_000.0,
        avg_volume50=1_000_000.0,
        volume_ratio=volume_ratio,
    )


def _trending_frame(
    bars: int = 200,
    latest_volume: float = 500_000.0,
    base_volume: float = 1_000_000.0,
    tail_spreads: tuple[float, float, float] = (10.0, 6.0, 2.0),
) -> pd.DataFrame:
    """A rising 200-bar chart whose final three weeks tighten on falling volume.

    Closes rise linearly, so the latest close sits above the 50-day mean, which
    sits above the 200-day mean.
    """
    closes = [50.0 + index * 0.5 for index in range(bars)]
    highs = [close + 20.0 for close in closes]
    lows = [close - 20.0 for close in closes]
    for week, spread in enumerate(tail_spreads):
        for offset in range(5):
            index = bars - 15 + week * 5 + offset
            highs[index] = closes[index] + spread
            lows[index] = closes[index]
    volumes = [base_volume] * (bars - 1) + [latest_volume]
    return pd.DataFrame({"High": highs, "Low": lows, "Close": closes, "Volume": volumes})


class TestWeeklyRangePcts:
    def test_each_week_is_its_span_over_its_close_oldest_first(self):
        # Oldest first, so a tightening base reads as a falling list — the order
        # the contraction rule compares. Reversed, every setup would invert.
        result = _weekly_range_pcts(_weeks_frame([10.0, 5.0, 2.0], close=100.0))

        assert result == [10.0, 5.0, 2.0]

    def test_only_the_trailing_weeks_are_measured(self):
        # A wide range four weeks back must not enter the comparison.
        result = _weekly_range_pcts(_weeks_frame([50.0, 10.0, 5.0, 2.0]))

        assert result == [10.0, 5.0, 2.0]

    @pytest.mark.parametrize("weeks", [0, 1, 2], ids=["empty", "one_week", "two_weeks"])
    def test_none_without_three_full_weeks(self, weeks: int):
        assert _weekly_range_pcts(_weeks_frame([4.0] * weeks)) is None

    def test_none_when_a_price_column_is_missing(self):
        frame = _weeks_frame([4.0, 3.0, 2.0]).drop(columns=["Low"])

        assert _weekly_range_pcts(frame) is None


class TestVcpLegs:
    @pytest.mark.parametrize(
        ("ranges", "expected"),
        [
            ([10.0, 5.0, 2.0], True),
            ([10.0, 10.0, 2.0], False),
            ([2.0, 5.0, 10.0], False),
            ([10.0, 2.0, 5.0], False),
            ([5.0], False),
        ],
        ids=["tightening", "flat_week", "expanding", "widens_last_week", "single_week"],
    )
    def test_contraction_requires_every_week_below_the_last(self, ranges: list[float], expected: bool):
        assert _snapshot(ranges).contraction_ok is expected

    @pytest.mark.parametrize(
        ("latest_close", "sma50", "sma200", "expected"),
        [
            (150.0, 130.0, 100.0, True),
            (120.0, 130.0, 100.0, False),
            (150.0, 90.0, 100.0, False),
            (130.0, 130.0, 100.0, False),
        ],
        ids=["stacked_uptrend", "close_below_fast", "fast_below_slow", "close_equals_fast"],
    )
    def test_trend_requires_a_stacked_uptrend(
        self, latest_close: float, sma50: float, sma200: float, expected: bool,
    ):
        assert _snapshot([10.0, 5.0, 2.0], latest_close, sma50, sma200).trend_ok is expected

    @pytest.mark.parametrize(
        ("volume_ratio", "expected"),
        [(0.5, True), (0.69, True), (0.7, False), (1.2, False)],
        ids=["dried_up", "just_inside", "exactly_at_threshold", "heavy_volume"],
    )
    def test_volume_dry_up_needs_more_than_a_30_percent_drop(self, volume_ratio: float, expected: bool):
        assert _snapshot([10.0, 5.0, 2.0], volume_ratio=volume_ratio).volume_dry_up is expected

    def test_all_three_legs_together_make_a_setup(self):
        assert _snapshot([10.0, 5.0, 2.0]).is_setup is True

    @pytest.mark.parametrize(
        "overrides",
        [
            {"ranges": [2.0, 5.0, 10.0]},
            {"latest_close": 120.0},
            {"volume_ratio": 1.1},
        ],
        ids=["no_contraction", "broken_trend", "no_volume_dry_up"],
    )
    def test_any_single_failed_leg_suppresses_the_setup(self, overrides: dict):
        params = {"ranges": [10.0, 5.0, 2.0]} | overrides

        assert _snapshot(**params).is_setup is False


class TestBuildVcpSnapshot:
    def test_a_tightening_uptrend_on_light_volume_is_a_setup(self):
        snapshot = _build_vcp_snapshot("test", _trending_frame())

        assert snapshot is not None
        assert snapshot.ticker == "TEST"
        assert snapshot.is_setup is True

    def test_heavy_closing_volume_is_not_a_setup(self):
        # Same chart, but the latest bar trades above its own 50-day average.
        snapshot = _build_vcp_snapshot("TEST", _trending_frame(latest_volume=2_000_000.0))

        assert snapshot is not None
        assert snapshot.volume_dry_up is False
        assert snapshot.is_setup is False

    def test_an_expanding_base_is_not_a_setup(self):
        snapshot = _build_vcp_snapshot("TEST", _trending_frame(tail_spreads=(2.0, 6.0, 10.0)))

        assert snapshot is not None
        assert snapshot.contraction_ok is False
        assert snapshot.is_setup is False

    def test_none_without_a_full_slow_sma_window(self):
        # The trend leg compares a 200-day mean, so a shorter history cannot
        # establish it and must not fall back to a shorter average.
        assert _build_vcp_snapshot("TEST", _trending_frame(bars=199)) is None

    def test_none_when_volume_is_missing(self):
        frame = _trending_frame().drop(columns=["Volume"])

        assert _build_vcp_snapshot("TEST", frame) is None
