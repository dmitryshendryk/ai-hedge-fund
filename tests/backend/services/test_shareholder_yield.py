"""Tests for true shareholder yield.

The sign convention is the whole risk here. yfinance reports a dividend payment
and a share issuance with opposite meanings but similar-looking numbers, so a
sign error would score a serial diluter as a capital returner — the exact
company this metric exists to exclude. Every case below states its inputs the
way the cash-flow statement does: outflows negative.
"""

import pytest

from app.backend.services.fundamentals_service._advanced import true_shareholder_yield


class TestTrueShareholderYield:
    def test_negative_outflows_read_as_capital_returned(self):
        # $80 of dividends against a $1,000 cap is an 8% dividend yield.
        result = true_shareholder_yield(market_cap=1_000.0, dividends_paid=-80.0)

        assert result.dividend_pct == 8.0
        assert result.total_pct == 8.0

    def test_positive_outflows_are_read_the_same_way(self):
        # Some vintages report the payment unsigned; the meaning is unchanged.
        result = true_shareholder_yield(market_cap=1_000.0, dividends_paid=80.0)

        assert result.dividend_pct == 8.0

    def test_all_three_routes_sum(self):
        result = true_shareholder_yield(
            market_cap=1_000.0,
            dividends_paid=-30.0,
            buybacks=-40.0,
            debt_repayment=-50.0,
        )

        assert result.dividend_pct == 3.0
        assert result.buyback_pct == 4.0
        assert result.debt_paydown_pct == 5.0
        assert result.total_pct == 12.0

    def test_issuance_is_subtracted_from_repurchases(self):
        # Bought back $50, issued $20 — only $30 left the company.
        result = true_shareholder_yield(
            market_cap=1_000.0, buybacks=-50.0, stock_issuance=20.0,
        )

        assert result.buyback_pct == 3.0

    def test_debt_issuance_is_subtracted_from_repayment(self):
        result = true_shareholder_yield(
            market_cap=1_000.0, debt_repayment=-100.0, debt_issuance=60.0,
        )

        assert result.debt_paydown_pct == 4.0

    @pytest.mark.parametrize(
        ("kwargs", "route"),
        [
            ({"buybacks": -20.0, "stock_issuance": 90.0}, "buyback_pct"),
            ({"debt_repayment": -20.0, "debt_issuance": 90.0}, "debt_paydown_pct"),
        ],
        ids=["net_share_issuer", "net_borrower"],
    )
    def test_a_net_issuer_scores_that_route_at_zero(self, kwargs: dict, route: str):
        result = true_shareholder_yield(market_cap=1_000.0, **kwargs)

        assert getattr(result, route) == 0.0

    def test_a_floored_route_does_not_offset_the_others(self):
        # Dilution must not cancel a real dividend; the routes are independent
        # claims on capital, not a single net figure.
        diluting = true_shareholder_yield(
            market_cap=1_000.0,
            dividends_paid=-80.0,
            buybacks=-10.0,
            stock_issuance=500.0,
        )

        assert diluting.buyback_pct == 0.0
        assert diluting.total_pct == 8.0

    def test_missing_rows_count_as_nothing_returned(self):
        # A company with no buyback line has not returned capital that way.
        result = true_shareholder_yield(market_cap=1_000.0, dividends_paid=-50.0)

        assert result.buyback_pct == 0.0
        assert result.debt_paydown_pct == 0.0
        assert result.total_pct == 5.0

    def test_a_company_returning_nothing_yields_zero(self):
        assert true_shareholder_yield(market_cap=1_000.0).total_pct == 0.0

    @pytest.mark.parametrize(
        "market_cap",
        [None, 0.0, -1_000.0],
        ids=["missing", "zero", "negative"],
    )
    def test_none_without_a_usable_market_cap(self, market_cap: float | None):
        # No denominator means no yield; a fabricated one would read as evidence.
        assert true_shareholder_yield(market_cap=market_cap, dividends_paid=-80.0) is None
