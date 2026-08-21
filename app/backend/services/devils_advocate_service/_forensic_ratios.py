"""Forensic ratio detectors: Montier C-Score and interest coverage.

Both read the shared ForensicBundle, so adding them costs no extra yfinance
fetch beyond what Altman Z and Beneish M already pay for.

The row-name candidates below overlap with the private copies in
_beneish_m_score, which yfinance's inconsistent statement labelling forces. A
later pass could lift them into _yfinance_fundamentals as one shared table.
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.devils_advocate_service._yfinance_fundamentals import (
    get_forensic_bundle,
    safe_row,
)
from app.backend.services.fundamentals_service._advanced import (
    croic,
    interest_coverage,
    montier_c_score,
)

logger = logging.getLogger(__name__)

_NET_INCOME = ("Net Income", "Net Income Common Stockholders")
_OPERATING_CASH_FLOW = ("Operating Cash Flow", "Total Cash From Operating Activities")
_FREE_CASH_FLOW = ("Free Cash Flow",)
_TOTAL_REVENUE = ("Total Revenue", "Revenue")
_RECEIVABLES = ("Net Receivables", "Receivables", "Accounts Receivable")
_INVENTORY = ("Inventory",)
_OTHER_CURRENT_ASSETS = ("Other Current Assets",)
_DEPRECIATION = ("Depreciation And Amortization", "Depreciation")
_GROSS_PPE = ("Gross PPE", "Properties Plant And Equipment Gross", "Net PPE")
_TOTAL_ASSETS = ("Total Assets",)
_EBIT = ("EBIT", "Operating Income", "Total Operating Income As Reported")
_INTEREST_EXPENSE = ("Interest Expense", "Interest Expense Non Operating")
_LONG_TERM_DEBT = ("Long Term Debt", "Long Term Debt And Capital Lease Obligation")
_EQUITY = ("Stockholders Equity", "Total Stockholder Equity", "Common Stock Equity")

# This band means most manipulation flags fired together, which Montier treats
# as high risk rather than ordinary accounting noise.
_C_SCORE_CRITICAL = 5
_C_SCORE_WARNING = 3

# Below this, one weak quarter leaves the company unable to service its debt.
_COVERAGE_CRITICAL = 1.5
_COVERAGE_WARNING = 3.0


def _year(bundle, col_idx: int) -> dict:
    """Line items for one fiscal year, keyed as montier_c_score expects."""
    return {
        "net_income": safe_row(bundle.income_statement, _NET_INCOME, col_idx),
        "operating_cash_flow": safe_row(bundle.cash_flow, _OPERATING_CASH_FLOW, col_idx),
        "revenue": safe_row(bundle.income_statement, _TOTAL_REVENUE, col_idx),
        "receivables": safe_row(bundle.balance_sheet, _RECEIVABLES, col_idx),
        "inventory": safe_row(bundle.balance_sheet, _INVENTORY, col_idx),
        "other_current_assets": safe_row(bundle.balance_sheet, _OTHER_CURRENT_ASSETS, col_idx),
        "depreciation": safe_row(bundle.cash_flow, _DEPRECIATION, col_idx),
        "gross_ppe": safe_row(bundle.balance_sheet, _GROSS_PPE, col_idx),
        "total_assets": safe_row(bundle.balance_sheet, _TOTAL_ASSETS, col_idx),
    }


async def detect_montier_c_score(ticker: str) -> list[RedFlagFinding]:
    """Flag accounting-manipulation signals. Returns [] when data is thin."""
    try:
        bundle = await get_forensic_bundle(ticker)
        if bundle is None:
            return []

        score = montier_c_score(current=_year(bundle, 0), prior=_year(bundle, 1))
        if score is None or score < _C_SCORE_WARNING:
            return []

        critical = score >= _C_SCORE_CRITICAL
        return [RedFlagFinding(
            detector="montier_c_score",
            score=60.0 if critical else 40.0,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            headline=f"Montier C-Score {score}/6 — accounting manipulation risk",
            detail={
                "ticker": ticker.upper(),
                "c_score": score,
                "critical_threshold": _C_SCORE_CRITICAL,
            },
        )]
    except Exception as exc:
        logger.debug("montier_c_score: detection failed for %s: %s", ticker, exc)
        return []


async def detect_interest_coverage(ticker: str) -> list[RedFlagFinding]:
    """Flag a company whose operating profit barely covers its interest bill.

    A debt-free company yields no ratio and no finding — absence of leverage is
    not a warning. CROIC rides along because it shares this fetch and shows
    whether the profit behind the coverage ever became cash.
    """
    try:
        bundle = await get_forensic_bundle(ticker)
        if bundle is None:
            return []

        coverage = interest_coverage(
            ebit=safe_row(bundle.income_statement, _EBIT, 0),
            interest_expense=safe_row(bundle.income_statement, _INTEREST_EXPENSE, 0),
        )
        if coverage is None or coverage >= _COVERAGE_WARNING:
            return []

        cash_return = croic(
            free_cash_flow=safe_row(bundle.cash_flow, _FREE_CASH_FLOW, 0),
            total_equity=safe_row(bundle.balance_sheet, _EQUITY, 0),
            long_term_debt=safe_row(bundle.balance_sheet, _LONG_TERM_DEBT, 0),
        )

        critical = coverage < _COVERAGE_CRITICAL
        headline = (
            f"Interest coverage {coverage:.2f}x — one weak quarter from default"
            if critical
            else f"Interest coverage {coverage:.2f}x — thin debt-service cushion"
        )
        return [RedFlagFinding(
            detector="interest_coverage",
            score=60.0 if critical else 40.0,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            headline=headline,
            detail={
                "ticker": ticker.upper(),
                "interest_coverage": coverage,
                "critical_threshold": _COVERAGE_CRITICAL,
                "croic_pct": cash_return,
            },
        )]
    except Exception as exc:
        logger.debug("interest_coverage: detection failed for %s: %s", ticker, exc)
        return []
