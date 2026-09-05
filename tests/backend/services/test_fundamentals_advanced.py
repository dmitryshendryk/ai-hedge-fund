"""Tests for the advanced fundamental metrics.

Each function is pure, so the arithmetic is pinned without touching yfinance.
Every metric returns None on missing or economically meaningless input rather
than a sentinel number: a fabricated ratio reads as real evidence downstream.
"""

import pytest

from app.backend.services.fundamentals_service._advanced import (
    croic,
    ev_to_fcf,
    interest_coverage,
    montier_c_score,
    rule_of_40,
)


class TestRuleOf40:
    """Growth plus margin. Below 40 the growth no longer pays for the burn."""

    @pytest.mark.parametrize(
        ("growth", "margin", "expected"),
        [
            (25.0, 20.0, 45.0),
            (60.0, -25.0, 35.0),
            (5.0, 35.0, 40.0),
            (-10.0, 15.0, 5.0),
        ],
        ids=["profitable_grower", "burning_for_growth", "exactly_40", "shrinking"],
    )
    def test_sums_growth_and_margin(self, growth: float, margin: float, expected: float):
        assert rule_of_40(revenue_growth_pct=growth, ebitda_margin_pct=margin) == expected

    @pytest.mark.parametrize(
        ("growth", "margin"),
        [(None, 20.0), (25.0, None), (None, None)],
        ids=["no_growth", "no_margin", "neither"],
    )
    def test_none_when_an_input_is_missing(self, growth: float | None, margin: float | None):
        assert rule_of_40(revenue_growth_pct=growth, ebitda_margin_pct=margin) is None


class TestEvToFcf:
    """What a buyer of the whole company pays per dollar of cash produced."""

    def test_divides_enterprise_value_by_free_cash_flow(self):
        assert ev_to_fcf(enterprise_value=150_000.0, free_cash_flow=10_000.0) == 15.0

    def test_negative_free_cash_flow_yields_none(self):
        # A cash-burning company has no meaningful multiple, and a negative
        # ratio would sort as "cheap" beside a genuinely cheap business.
        assert ev_to_fcf(enterprise_value=150_000.0, free_cash_flow=-5_000.0) is None

    @pytest.mark.parametrize(
        ("ev", "fcf"),
        [(None, 10_000.0), (150_000.0, None), (150_000.0, 0.0), (-10.0, 10_000.0)],
        ids=["no_ev", "no_fcf", "zero_fcf", "negative_ev"],
    )
    def test_none_on_unusable_input(self, ev: float | None, fcf: float | None):
        assert ev_to_fcf(enterprise_value=ev, free_cash_flow=fcf) is None


class TestCroic:
    """Cash return on invested capital: FCF over equity plus long-term debt."""

    def test_returns_percent_of_invested_capital(self):
        assert croic(free_cash_flow=15_000.0, total_equity=100_000.0, long_term_debt=50_000.0) == 10.0

    def test_negative_free_cash_flow_gives_a_negative_return(self):
        # Unlike a valuation multiple, a negative cash return is meaningful.
        assert croic(free_cash_flow=-15_000.0, total_equity=100_000.0, long_term_debt=50_000.0) == -10.0

    def test_missing_long_term_debt_counts_as_none_outstanding(self):
        assert croic(free_cash_flow=10_000.0, total_equity=100_000.0, long_term_debt=None) == 10.0

    @pytest.mark.parametrize(
        ("fcf", "equity", "debt"),
        [
            (None, 100_000.0, 0.0),
            (10_000.0, None, 0.0),
            (10_000.0, 0.0, 0.0),
            (10_000.0, -60_000.0, 50_000.0),
        ],
        ids=["no_fcf", "no_equity", "zero_capital", "negative_capital"],
    )
    def test_none_on_unusable_input(self, fcf: float | None, equity: float | None, debt: float | None):
        assert croic(free_cash_flow=fcf, total_equity=equity, long_term_debt=debt) is None


class TestInterestCoverage:
    """How many times operating profit covers the interest bill."""

    def test_divides_ebit_by_interest_expense(self):
        assert interest_coverage(ebit=500.0, interest_expense=100.0) == 5.0

    def test_interest_expense_reported_negative_is_treated_as_a_cost(self):
        # yfinance signs interest expense inconsistently across tickers.
        assert interest_coverage(ebit=500.0, interest_expense=-100.0) == 5.0

    def test_operating_loss_yields_negative_coverage(self):
        assert interest_coverage(ebit=-200.0, interest_expense=100.0) == -2.0

    @pytest.mark.parametrize(
        ("ebit", "interest"),
        [(None, 100.0), (500.0, None), (500.0, 0.0)],
        ids=["no_ebit", "no_interest", "debt_free"],
    )
    def test_none_when_coverage_is_undefined(self, ebit: float | None, interest: float | None):
        # A debt-free company has no coverage ratio, which is not a warning.
        assert interest_coverage(ebit=ebit, interest_expense=interest) is None


def _clean_year() -> dict:
    """A year that trips no C-Score flag, used as the baseline to perturb."""
    return {
        "net_income": 80.0,
        "operating_cash_flow": 120.0,
        "revenue": 1_000.0,
        "receivables": 100.0,
        "inventory": 100.0,
        "other_current_assets": 50.0,
        "depreciation": 100.0,
        "gross_ppe": 1_000.0,
        "total_assets": 1_000.0,
    }


class TestMontierCScore:
    """Accounting-manipulation flags, one point each, so the range is 0-6."""

    def test_clean_books_score_zero(self):
        assert montier_c_score(current=_clean_year(), prior=_clean_year()) == 0

    def test_earnings_above_cash_flow_scores_a_point(self):
        current = _clean_year() | {"net_income": 200.0, "operating_cash_flow": 120.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_rising_days_sales_outstanding_scores_a_point(self):
        # Receivables growing faster than revenue suggests channel stuffing.
        current = _clean_year() | {"receivables": 150.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_rising_days_inventory_scores_a_point(self):
        current = _clean_year() | {"inventory": 150.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_rising_other_current_assets_scores_a_point(self):
        current = _clean_year() | {"other_current_assets": 90.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_falling_depreciation_rate_scores_a_point(self):
        # Slower depreciation on the same asset base lifts reported profit.
        current = _clean_year() | {"depreciation": 60.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_asset_growth_above_ten_percent_scores_a_point(self):
        current = _clean_year() | {"total_assets": 1_200.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 1

    def test_asset_growth_at_ten_percent_does_not_score(self):
        current = _clean_year() | {"total_assets": 1_100.0}

        assert montier_c_score(current=current, prior=_clean_year()) == 0

    def test_every_flag_together_scores_six(self):
        current = {
            "net_income": 300.0,
            "operating_cash_flow": 50.0,
            "revenue": 1_000.0,
            "receivables": 200.0,
            "inventory": 200.0,
            "other_current_assets": 120.0,
            "depreciation": 40.0,
            "gross_ppe": 1_000.0,
            "total_assets": 1_500.0,
        }

        assert montier_c_score(current=current, prior=_clean_year()) == 6

    def test_none_when_the_prior_year_is_missing(self):
        assert montier_c_score(current=_clean_year(), prior=None) is None

    def test_flags_with_missing_inputs_are_skipped_not_counted(self):
        # Thin coverage must read as neither a clean book nor a flag.
        sparse = {"net_income": 200.0, "operating_cash_flow": 120.0}

        assert montier_c_score(current=sparse, prior=sparse) == 1
