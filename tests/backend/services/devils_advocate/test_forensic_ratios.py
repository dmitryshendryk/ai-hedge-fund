"""Tests for the forensic ratio detectors.

Both must stay silent on thin data. A red-flag badge asserts evidence, so an
absent statement row must never produce one.
"""

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.backend.services.devils_advocate_service import _forensic_ratios
from app.backend.services.devils_advocate_service._forensic_ratios import (
    detect_interest_coverage,
    detect_montier_c_score,
)
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.devils_advocate_service._yfinance_fundamentals import ForensicBundle


def _frame(rows: dict[str, list[float]]) -> pd.DataFrame:
    # Columns are fiscal years, most recent first, matching yfinance.
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, index=["y0", "y1"]).T


def _bundle(balance: dict, income: dict, cash: dict) -> ForensicBundle:
    return ForensicBundle(
        ticker="TEST",
        balance_sheet=_frame(balance),
        income_statement=_frame(income),
        cash_flow=_frame(cash),
        sector="Technology",
        market_cap=1_000_000.0,
    )


def _manipulating_bundle() -> ForensicBundle:
    # Every flag fires: earnings above cash, all three asset ratios rising,
    # depreciation slowing, assets up 50%.
    return _bundle(
        balance={
            "Total Assets": [1_500.0, 1_000.0],
            "Net Receivables": [200.0, 100.0],
            "Inventory": [200.0, 100.0],
            "Other Current Assets": [120.0, 50.0],
            "Gross PPE": [1_000.0, 1_000.0],
        },
        income={"Net Income": [300.0, 80.0], "Total Revenue": [1_000.0, 1_000.0]},
        cash={"Operating Cash Flow": [50.0, 120.0], "Depreciation And Amortization": [40.0, 100.0]},
    )


def _clean_bundle() -> ForensicBundle:
    return _bundle(
        balance={
            "Total Assets": [1_000.0, 1_000.0],
            "Net Receivables": [100.0, 100.0],
            "Inventory": [100.0, 100.0],
            "Other Current Assets": [50.0, 50.0],
            "Gross PPE": [1_000.0, 1_000.0],
        },
        income={"Net Income": [80.0, 80.0], "Total Revenue": [1_000.0, 1_000.0]},
        cash={"Operating Cash Flow": [120.0, 120.0], "Depreciation And Amortization": [100.0, 100.0]},
    )


def _levered_bundle(ebit: float, interest: float) -> ForensicBundle:
    return _bundle(
        balance={"Stockholders Equity": [1_000.0, 1_000.0], "Long Term Debt": [500.0, 500.0]},
        income={"EBIT": [ebit, ebit], "Interest Expense": [interest, interest]},
        cash={"Free Cash Flow": [150.0, 150.0]},
    )


class TestMontierCScoreDetector:
    @pytest.mark.asyncio
    async def test_manipulating_books_raise_a_critical_flag(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=_manipulating_bundle())):
            findings = await detect_montier_c_score("TEST")

        assert len(findings) == 1
        assert findings[0].severity is Severity.CRITICAL
        assert findings[0].detail["c_score"] == 6

    @pytest.mark.asyncio
    async def test_clean_books_raise_nothing(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=_clean_bundle())):
            assert await detect_montier_c_score("TEST") == []

    @pytest.mark.asyncio
    async def test_no_flag_without_a_bundle(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=None)):
            assert await detect_montier_c_score("TEST") == []

    @pytest.mark.asyncio
    async def test_fetch_failure_is_swallowed(self):
        # A detector must never raise into the report's gather.
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(side_effect=RuntimeError("down"))):
            assert await detect_montier_c_score("TEST") == []


class TestInterestCoverageDetector:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("ebit", "interest", "expected_severity"),
        [
            (100.0, 100.0, Severity.CRITICAL),
            (140.0, 100.0, Severity.CRITICAL),
            (200.0, 100.0, Severity.WARNING),
            (-50.0, 100.0, Severity.CRITICAL),
        ],
        ids=["coverage_1x", "just_below_critical", "thin_cushion", "operating_loss"],
    )
    async def test_thin_coverage_is_flagged_by_tier(self, ebit: float, interest: float, expected_severity: Severity):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=_levered_bundle(ebit, interest))):
            findings = await detect_interest_coverage("TEST")

        assert len(findings) == 1
        assert findings[0].severity is expected_severity

    @pytest.mark.asyncio
    async def test_comfortable_coverage_raises_nothing(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=_levered_bundle(800.0, 100.0))):
            assert await detect_interest_coverage("TEST") == []

    @pytest.mark.asyncio
    async def test_a_debt_free_company_raises_nothing(self):
        # No interest owed means no coverage ratio, which is not a warning.
        bundle = _bundle(
            balance={"Stockholders Equity": [1_000.0, 1_000.0]},
            income={"EBIT": [10.0, 10.0], "Interest Expense": [0.0, 0.0]},
            cash={"Free Cash Flow": [150.0, 150.0]},
        )
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=bundle)):
            assert await detect_interest_coverage("TEST") == []

    @pytest.mark.asyncio
    async def test_croic_rides_along_in_the_detail(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(return_value=_levered_bundle(100.0, 100.0))):
            findings = await detect_interest_coverage("TEST")

        assert findings[0].detail["croic_pct"] == 10.0

    @pytest.mark.asyncio
    async def test_fetch_failure_is_swallowed(self):
        with patch.object(_forensic_ratios, "get_forensic_bundle", AsyncMock(side_effect=RuntimeError("down"))):
            assert await detect_interest_coverage("TEST") == []
