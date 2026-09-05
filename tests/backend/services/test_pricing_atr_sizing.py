"""Tests for ATR and volatility-based position sizing.

ATR sizing exists so a fixed dollar risk buys fewer shares of a volatile name
than of a quiet one. The tests pin that inverse relationship, because getting it
backwards would size the riskiest positions largest.
"""

import pandas as pd
import pytest

from app.backend.services.pricing_service import _compute_atr, suggest_position_size


def _bars(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"High": highs, "Low": lows, "Close": closes})


def _flat_range_bars(count: int, spread: float, close: float = 100.0) -> pd.DataFrame:
    # Every bar has the same high-low spread and an unchanged close, so true
    # range equals the spread on every bar and ATR converges to it exactly.
    return _bars([close + spread / 2] * count, [close - spread / 2] * count, [close] * count)


def test_atr_of_constant_range_equals_that_range():
    atr = _compute_atr(_flat_range_bars(60, spread=4.0), period=14)

    assert atr == pytest.approx(4.0, abs=0.01)


def test_atr_rises_with_a_wider_range():
    quiet = _compute_atr(_flat_range_bars(60, spread=2.0), period=14)
    volatile = _compute_atr(_flat_range_bars(60, spread=8.0), period=14)

    assert volatile > quiet


def test_atr_includes_the_overnight_gap():
    # A gap up leaves the intraday spread small while the move from the prior
    # close is large, so true range must consider the prior close.
    gapped = _bars(
        highs=[100.0] * 30 + [130.0],
        lows=[99.0] * 30 + [129.0],
        closes=[99.5] * 30 + [129.5],
    )
    intraday_only = _bars([100.0] * 31, [99.0] * 31, [99.5] * 31)

    assert _compute_atr(gapped, period=14) > _compute_atr(intraday_only, period=14)


@pytest.mark.parametrize("count", [0, 1, 14], ids=["empty", "single_bar", "exactly_period"])
def test_atr_returns_none_without_enough_bars(count: int):
    # True range needs a prior close, so period bars yield only period-1 values.
    assert _compute_atr(_flat_range_bars(count, spread=2.0), period=14) is None


def test_atr_returns_none_when_columns_are_missing():
    assert _compute_atr(pd.DataFrame({"Close": [1.0, 2.0, 3.0]}), period=14) is None


def test_atr_ignores_rows_with_gaps_in_the_data():
    frame = _flat_range_bars(60, spread=4.0)
    frame.loc[5, "High"] = None

    assert _compute_atr(frame, period=14) == pytest.approx(4.0, abs=0.2)


class TestSuggestPositionSize:
    """Sizing arithmetic: shares = (account * risk_pct) / (stop_multiple * ATR)."""

    def test_risk_budget_divided_by_stop_distance_gives_shares(self):
        # $20k at 2% risks $400; a $4 ATR at 1.5x is a $6 stop distance.
        result = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=4.0)

        assert result.risk_amount == 400.0
        assert result.stop_distance == 6.0
        assert result.shares == 66

    def test_a_volatile_name_gets_fewer_shares_than_a_quiet_one(self):
        quiet = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=2.0)
        volatile = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=20.0)

        assert volatile.shares < quiet.shares

    def test_shares_round_down_so_risk_is_never_exceeded(self):
        # 400 / 6 is 66.67; rounding up would risk more than the stated budget.
        assert suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=4.0).shares == 66

    def test_position_value_reported_when_price_supplied(self):
        result = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=4.0, price=150.0)

        assert result.position_value == pytest.approx(9900.0)
        assert result.position_pct_of_account == pytest.approx(49.5)

    def test_position_value_is_none_without_a_price(self):
        assert suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=4.0).position_value is None

    def test_atr_too_small_to_size_yields_zero_shares(self):
        # Guards the divide-by-zero rather than returning an unbounded size.
        assert suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=0.0).shares == 0

    def test_a_quiet_name_is_capped_by_available_capital(self):
        # Risk alone permits 266 shares at $120, which is 160% of a $20k account.
        result = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=1.0, price=120.0)

        assert result.shares == 266
        assert result.capped_shares == 166
        assert result.capped_by == "capital"

    def test_a_volatile_name_stays_bound_by_risk(self):
        result = suggest_position_size(account_value=20_000.0, risk_pct=0.02, atr=4.0, price=120.0)

        assert result.capped_shares == result.shares
        assert result.capped_by == "risk"

    @pytest.mark.parametrize(
        ("account_value", "risk_pct", "message"),
        [
            (0.0, 0.02, "Account value"),
            (-100.0, 0.02, "Account value"),
            (20_000.0, 0.0, "Risk percent"),
            (20_000.0, -0.01, "Risk percent"),
            (20_000.0, 1.5, "Risk percent"),
        ],
        ids=["zero_account", "negative_account", "zero_risk", "negative_risk", "risk_above_one"],
    )
    def test_invalid_inputs_are_rejected(self, account_value: float, risk_pct: float, message: str):
        with pytest.raises(ValueError, match=message):
            suggest_position_size(account_value=account_value, risk_pct=risk_pct, atr=4.0)
