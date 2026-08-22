"""Tests for the Piotroski F-Score and the DuPont leverage-trap check.

Both compare a current fiscal year against the prior one. A test whose inputs
are missing must not score a point, so thin coverage cannot masquerade as
either strength or distress.
"""

import pytest

from app.backend.services.fundamentals_service._advanced import (
    dupont_breakdown,
    dupont_leverage_trap,
    piotroski_score,
)


def _strong_prior() -> dict:
    return {
        "net_income": 100.0,
        "operating_cash_flow": 150.0,
        "total_assets": 1_000.0,
        "revenue": 1_000.0,
        "long_term_debt": 200.0,
        "current_assets": 400.0,
        "current_liabilities": 200.0,
        "shares_outstanding": 100.0,
        "gross_profit": 400.0,
        "total_equity": 500.0,
    }


def _strong_current() -> dict:
    """Passes all nine tests against _strong_prior."""
    return {
        "net_income": 200.0,           # ROA 20% > 0 and above prior 10%
        "operating_cash_flow": 300.0,  # positive, and above net income
        "total_assets": 1_000.0,
        "revenue": 1_200.0,            # turnover 1.2 > prior 1.0
        "long_term_debt": 100.0,       # leverage 10% < prior 20%
        "current_assets": 600.0,       # current ratio 3.0 > prior 2.0
        "current_liabilities": 200.0,
        "shares_outstanding": 100.0,   # no dilution
        "gross_profit": 600.0,         # margin 50% > prior 40%
        "total_equity": 600.0,
    }


class TestPiotroskiScore:
    def test_a_strong_year_scores_nine(self):
        assert piotroski_score(current=_strong_current(), prior=_strong_prior()) == 9

    def test_a_distressed_year_scores_zero(self):
        current = {
            "net_income": -50.0,           # ROA negative
            "operating_cash_flow": -80.0,  # negative, and below net income
            "total_assets": 1_200.0,
            "revenue": 800.0,              # turnover 0.67 < prior 1.0
            "long_term_debt": 400.0,       # leverage 33% > prior 20%
            "current_assets": 300.0,       # current ratio 1.5 < prior 2.0
            "current_liabilities": 200.0,
            "shares_outstanding": 130.0,   # dilution
            "gross_profit": 200.0,         # margin 25% < prior 40%
            "total_equity": 400.0,
        }

        assert piotroski_score(current=current, prior=_strong_prior()) == 0

    @pytest.mark.parametrize(
        ("overrides", "expected"),
        [
            ({"net_income": -50.0}, 7),
            ({"operating_cash_flow": -10.0}, 7),
            ({"long_term_debt": 400.0}, 8),
            ({"current_assets": 300.0}, 8),
            ({"shares_outstanding": 120.0}, 8),
            ({"gross_profit": 300.0}, 8),
            ({"revenue": 900.0}, 8),
        ],
        ids=[
            "loss_costs_roa_and_its_trend",
            "cash_burn_costs_cfo_and_accruals",
            "rising_leverage",
            "falling_liquidity",
            "dilution",
            "margin_contraction",
            "falling_turnover",
        ],
    )
    def test_each_failed_test_costs_its_points(self, overrides: dict, expected: int):
        current = _strong_current() | overrides

        assert piotroski_score(current=current, prior=_strong_prior()) == expected

    def test_none_when_the_prior_year_is_missing(self):
        assert piotroski_score(current=_strong_current(), prior=None) is None

    def test_missing_inputs_score_no_points_rather_than_free_ones(self):
        # Only the CFO-positive test can be evaluated from this much data.
        sparse = {"operating_cash_flow": 10.0}

        assert piotroski_score(current=sparse, prior=sparse) == 1


class TestDupontBreakdown:
    def test_factors_multiply_back_to_roe(self):
        result = dupont_breakdown(
            net_income=200.0, revenue=1_000.0, total_assets=2_000.0, total_equity=1_000.0,
        )

        assert result.net_profit_margin_pct == 20.0
        assert result.asset_turnover == 0.5
        assert result.equity_multiplier == 2.0
        assert result.roe_pct == pytest.approx(20.0)

    def test_none_when_equity_is_not_positive(self):
        # A negative book value makes the multiplier meaningless.
        assert dupont_breakdown(
            net_income=200.0, revenue=1_000.0, total_assets=2_000.0, total_equity=-100.0,
        ) is None

    def test_none_on_missing_input(self):
        assert dupont_breakdown(
            net_income=None, revenue=1_000.0, total_assets=2_000.0, total_equity=1_000.0,
        ) is None


class TestDupontLeverageTrap:
    """High ROE held up by borrowing while margins fall."""

    def _trap_years(self) -> tuple[dict, dict]:
        prior = {"net_income": 150.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 800.0}
        # Margin 12% < 15%, multiplier 2.5 against 1.25, ROE 30%.
        current = {"net_income": 120.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 400.0}
        return current, prior

    def test_fires_when_roe_is_propped_up_by_leverage(self):
        current, prior = self._trap_years()

        assert dupont_leverage_trap(current=current, prior=prior) is True

    def test_silent_when_margins_are_improving(self):
        current, prior = self._trap_years()
        current = current | {"net_income": 300.0}  # margin 30% > prior 15%

        assert dupont_leverage_trap(current=current, prior=prior) is False

    def test_silent_when_leverage_is_flat(self):
        prior = {"net_income": 150.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 400.0}
        current = {"net_income": 120.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 400.0}

        assert dupont_leverage_trap(current=current, prior=prior) is False

    def test_silent_when_roe_is_unremarkable(self):
        # Falling margins and rising debt, but ROE never reaches the 15% floor,
        # so there is no flattering headline number to mistrust.
        prior = {"net_income": 20.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 800.0}
        current = {"net_income": 10.0, "revenue": 1_000.0, "total_assets": 1_000.0, "total_equity": 400.0}

        assert dupont_leverage_trap(current=current, prior=prior) is False

    def test_none_when_a_year_cannot_be_decomposed(self):
        assert dupont_leverage_trap(current={"net_income": 1.0}, prior=None) is None
