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
    dupont_breakdown,
    dupont_leverage_trap,
    interest_coverage,
    montier_c_score,
    piotroski_score,
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
_CURRENT_ASSETS = ("Current Assets", "Total Current Assets")
_CURRENT_LIABILITIES = ("Current Liabilities", "Total Current Liabilities")
_SHARES_OUTSTANDING = ("Ordinary Shares Number", "Share Issued", "Common Stock Shares Outstanding")
_GROSS_PROFIT = ("Gross Profit",)

# This band means most manipulation flags fired together, which Montier treats
# as high risk rather than ordinary accounting noise.
_C_SCORE_CRITICAL = 5
_C_SCORE_WARNING = 3

# Below this, one weak quarter leaves the company unable to service its debt.
_COVERAGE_CRITICAL = 1.5
_COVERAGE_WARNING = 3.0

# Piotroski bands: 0-3 marks a deteriorating business on every axis the score
# measures, 4-5 a weak one. 6 and above is not a bear signal and stays silent.
_F_SCORE_CRITICAL = 3
_F_SCORE_WARNING = 5


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


def _piotroski_year(bundle, col_idx: int) -> dict:
    """Line items for one fiscal year, keyed as piotroski_score expects."""
    return {
        "net_income": safe_row(bundle.income_statement, _NET_INCOME, col_idx),
        "operating_cash_flow": safe_row(bundle.cash_flow, _OPERATING_CASH_FLOW, col_idx),
        "total_assets": safe_row(bundle.balance_sheet, _TOTAL_ASSETS, col_idx),
        "revenue": safe_row(bundle.income_statement, _TOTAL_REVENUE, col_idx),
        "long_term_debt": safe_row(bundle.balance_sheet, _LONG_TERM_DEBT, col_idx),
        "current_assets": safe_row(bundle.balance_sheet, _CURRENT_ASSETS, col_idx),
        "current_liabilities": safe_row(bundle.balance_sheet, _CURRENT_LIABILITIES, col_idx),
        "shares_outstanding": safe_row(bundle.balance_sheet, _SHARES_OUTSTANDING, col_idx),
        "gross_profit": safe_row(bundle.income_statement, _GROSS_PROFIT, col_idx),
    }


def _dupont_year(bundle, col_idx: int) -> dict:
    return {
        "net_income": safe_row(bundle.income_statement, _NET_INCOME, col_idx),
        "revenue": safe_row(bundle.income_statement, _TOTAL_REVENUE, col_idx),
        "total_assets": safe_row(bundle.balance_sheet, _TOTAL_ASSETS, col_idx),
        "total_equity": safe_row(bundle.balance_sheet, _EQUITY, col_idx),
    }


async def detect_piotroski_distress(ticker: str) -> list[RedFlagFinding]:
    """Flag a weak Piotroski F-Score. A healthy score raises nothing.

    This is the bear side of the score only: 6 and above is silent, because a
    strong business is not a red flag.
    """
    try:
        bundle = await get_forensic_bundle(ticker)
        if bundle is None:
            return []

        score = piotroski_score(current=_piotroski_year(bundle, 0), prior=_piotroski_year(bundle, 1))
        if score is None or score > _F_SCORE_WARNING:
            return []

        critical = score <= _F_SCORE_CRITICAL
        return [RedFlagFinding(
            detector="piotroski_distress",
            score=60.0 if critical else 40.0,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            headline=f"Piotroski F-Score {score}/9 — deteriorating fundamentals",
            detail={
                "ticker": ticker.upper(),
                "f_score": score,
                "critical_at_or_below": _F_SCORE_CRITICAL,
            },
        )]
    except Exception as exc:
        logger.debug("piotroski_distress: detection failed for %s: %s", ticker, exc)
        return []


async def detect_dupont_leverage_trap(ticker: str) -> list[RedFlagFinding]:
    """Flag a high ROE that borrowing, not trading, is holding up."""
    try:
        bundle = await get_forensic_bundle(ticker)
        if bundle is None:
            return []

        current = _dupont_year(bundle, 0)
        prior = _dupont_year(bundle, 1)
        if not dupont_leverage_trap(current=current, prior=prior):
            return []

        now = dupont_breakdown(**current)
        before = dupont_breakdown(**prior)
        return [RedFlagFinding(
            detector="dupont_leverage_trap",
            score=60.0,
            severity=Severity.CRITICAL,
            headline=(
                f"ROE {now.roe_pct:.0f}% is leverage-driven — margin fell to "
                f"{now.net_profit_margin_pct:.1f}% as debt rose"
            ),
            detail={
                "ticker": ticker.upper(),
                "roe_pct": now.roe_pct,
                "net_profit_margin_pct": now.net_profit_margin_pct,
                "prior_net_profit_margin_pct": before.net_profit_margin_pct,
                "equity_multiplier": now.equity_multiplier,
                "prior_equity_multiplier": before.equity_multiplier,
                "asset_turnover": now.asset_turnover,
            },
        )]
    except Exception as exc:
        logger.debug("dupont_leverage_trap: detection failed for %s: %s", ticker, exc)
        return []


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
