"""Advanced fundamental metrics: valuation, cash return, solvency, and
accounting-manipulation scoring.

Pure arithmetic only — callers supply line items they already fetched. Every
function returns None when an input is missing or the result would be
economically meaningless, because a fabricated ratio reads as real evidence in a
screen or a red-flag badge.
"""

import math
from dataclasses import dataclass

# Montier C-Score flag: balance-sheet growth beyond this rate reads as a
# manipulation signal rather than ordinary expansion.
_ASSET_GROWTH_FLAG_PCT: float = 10.0

# DuPont leverage trap: an ROE worth mistrusting, and the rise in the equity
# multiplier that suggests borrowing rather than trading is holding it up.
_TRAP_MIN_ROE_PCT: float = 15.0
_TRAP_MIN_LEVERAGE_RISE_PCT: float = 15.0


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rule_of_40(revenue_growth_pct: float | None, ebitda_margin_pct: float | None) -> float | None:
    """Revenue growth plus EBITDA margin, both in percent.

    A fast grower may burn cash and a slow one may not. Below 40 the growth no
    longer pays for the burn.
    """
    growth = _finite(revenue_growth_pct)
    margin = _finite(ebitda_margin_pct)
    if growth is None or margin is None:
        return None
    return round(growth + margin, 2)


def ev_to_fcf(enterprise_value: float | None, free_cash_flow: float | None) -> float | None:
    """Enterprise value per dollar of free cash flow.

    Returns:
        None when free cash flow is not positive; a negative multiple would sort
        as cheap beside a genuinely cheap business.
    """
    ev = _finite(enterprise_value)
    fcf = _finite(free_cash_flow)
    if ev is None or fcf is None or fcf <= 0 or ev <= 0:
        return None
    return round(ev / fcf, 2)


def croic(
    free_cash_flow: float | None,
    total_equity: float | None,
    long_term_debt: float | None,
) -> float | None:
    """Cash return on invested capital, in percent.

    Uses free cash flow rather than the accounting profit behind ROIC, so a high
    ROIC beside a low CROIC exposes earnings that never became cash.

    Args:
        long_term_debt: None is treated as none outstanding.

    Returns:
        None when invested capital is not positive; the ratio has no meaning
        against a negative capital base.
    """
    fcf = _finite(free_cash_flow)
    equity = _finite(total_equity)
    if fcf is None or equity is None:
        return None
    invested = equity + (_finite(long_term_debt) or 0.0)
    if invested <= 0:
        return None
    return round(fcf / invested * 100.0, 2)


def interest_coverage(ebit: float | None, interest_expense: float | None) -> float | None:
    """Times operating profit covers the interest bill.

    Args:
        interest_expense: Sign is ignored; yfinance reports it inconsistently.

    Returns:
        None when no interest is owed. A debt-free company has no coverage
        ratio, which is not a warning.
    """
    operating_profit = _finite(ebit)
    interest = _finite(interest_expense)
    if operating_profit is None or interest is None:
        return None
    interest = abs(interest)
    if interest <= 0:
        return None
    return round(operating_profit / interest, 2)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    num = _finite(numerator)
    den = _finite(denominator)
    if num is None or den is None or den <= 0:
        return None
    return num / den


def _rose(current: float | None, prior: float | None) -> bool:
    return current is not None and prior is not None and current > prior


def piotroski_score(current: dict | None, prior: dict | None) -> int | None:
    """Piotroski F-Score: nine pass/fail tests of financial trend health, 0-9.

    Args:
        current: Latest fiscal year line items — net_income, operating_cash_flow,
            total_assets, revenue, long_term_debt, current_assets,
            current_liabilities, shares_outstanding, gross_profit. An absent key
            fails its test.
        prior: The preceding year in the same shape.

    Returns:
        None when either year is absent. 7-9 marks a strengthening business,
        0-3 distress. A test whose inputs are missing scores no point, so thin
        coverage never reads as strength.
    """
    if not current or not prior:
        return None

    score = 0
    roa_now = _ratio(current.get("net_income"), current.get("total_assets"))
    roa_before = _ratio(prior.get("net_income"), prior.get("total_assets"))
    cash_flow = _finite(current.get("operating_cash_flow"))
    net_income = _finite(current.get("net_income"))

    # Profitability.
    if roa_now is not None and roa_now > 0:
        score += 1
    if cash_flow is not None and cash_flow > 0:
        score += 1
    if _rose(roa_now, roa_before):
        score += 1
    # Cash exceeding accounting profit means the earnings are real.
    if cash_flow is not None and net_income is not None and cash_flow > net_income:
        score += 1

    # Funding and liquidity. Falling leverage and rising liquidity score.
    leverage_now = _ratio(current.get("long_term_debt"), current.get("total_assets"))
    leverage_before = _ratio(prior.get("long_term_debt"), prior.get("total_assets"))
    if leverage_now is not None and leverage_before is not None and leverage_now < leverage_before:
        score += 1
    if _rose(
        _ratio(current.get("current_assets"), current.get("current_liabilities")),
        _ratio(prior.get("current_assets"), prior.get("current_liabilities")),
    ):
        score += 1
    shares_now = _finite(current.get("shares_outstanding"))
    shares_before = _finite(prior.get("shares_outstanding"))
    if shares_now is not None and shares_before is not None and shares_now <= shares_before:
        score += 1

    # Operating efficiency.
    if _rose(
        _ratio(current.get("gross_profit"), current.get("revenue")),
        _ratio(prior.get("gross_profit"), prior.get("revenue")),
    ):
        score += 1
    if _rose(
        _ratio(current.get("revenue"), current.get("total_assets")),
        _ratio(prior.get("revenue"), prior.get("total_assets")),
    ):
        score += 1

    return score


@dataclass(frozen=True)
class DupontBreakdown:
    """ROE split into the three factors that produce it.

    roe_pct is the product of the factors, so comparing them across years shows
    whether trading or borrowing moved the headline return.
    """
    net_profit_margin_pct: float
    asset_turnover: float
    equity_multiplier: float
    roe_pct: float


def dupont_breakdown(
    net_income: float | None,
    revenue: float | None,
    total_assets: float | None,
    total_equity: float | None,
) -> DupontBreakdown | None:
    """Decompose ROE into margin, asset turnover and equity multiplier.

    Returns:
        None when equity is not positive; a negative book value makes the
        multiplier meaningless rather than merely large.
    """
    margin = _ratio(net_income, revenue)
    turnover = _ratio(revenue, total_assets)
    multiplier = _ratio(total_assets, total_equity)
    if margin is None or turnover is None or multiplier is None:
        return None
    return DupontBreakdown(
        net_profit_margin_pct=round(margin * 100.0, 2),
        asset_turnover=round(turnover, 4),
        equity_multiplier=round(multiplier, 4),
        roe_pct=round(margin * turnover * multiplier * 100.0, 2),
    )


def dupont_leverage_trap(current: dict | None, prior: dict | None) -> bool | None:
    """True when a flattering ROE rests on borrowing while margins fall.

    Args:
        current: Latest fiscal year — net_income, revenue, total_assets,
            total_equity.
        prior: The preceding year in the same shape.

    Returns:
        None when either year cannot be decomposed. The three conditions must
        hold together: an ROE high enough to attract a buyer, a margin below
        last year's, and an equity multiplier materially above it.
    """
    if not current or not prior:
        return None

    now = dupont_breakdown(
        net_income=current.get("net_income"),
        revenue=current.get("revenue"),
        total_assets=current.get("total_assets"),
        total_equity=current.get("total_equity"),
    )
    before = dupont_breakdown(
        net_income=prior.get("net_income"),
        revenue=prior.get("revenue"),
        total_assets=prior.get("total_assets"),
        total_equity=prior.get("total_equity"),
    )
    if now is None or before is None or before.equity_multiplier <= 0:
        return None

    leverage_rise_pct = (now.equity_multiplier / before.equity_multiplier - 1.0) * 100.0
    return (
        now.roe_pct > _TRAP_MIN_ROE_PCT
        and now.net_profit_margin_pct < before.net_profit_margin_pct
        and leverage_rise_pct >= _TRAP_MIN_LEVERAGE_RISE_PCT
    )


def montier_c_score(current: dict | None, prior: dict | None) -> int | None:
    """Montier C-Score: accounting-manipulation flags, one point each, 0-6.

    Args:
        current: Latest fiscal year line items — net_income, operating_cash_flow,
            revenue, receivables, inventory, other_current_assets, depreciation,
            gross_ppe, total_assets. An absent key skips its flag.
        prior: The preceding year in the same shape.

    Returns:
        None when either year is absent. A flag whose inputs are missing is
        skipped rather than counted, so thin coverage reads as neither a clean
        book nor a manipulation signal.
    """
    if not current or not prior:
        return None

    score = 0

    # Earnings outrunning cash is the headline divergence the score exists for.
    net_income = _finite(current.get("net_income"))
    cash_flow = _finite(current.get("operating_cash_flow"))
    if net_income is not None and cash_flow is not None and net_income > cash_flow:
        score += 1

    # Each asset growing faster than sales: revenue booked but not collected,
    # goods piling up unsold, or costs parked outside the income statement.
    for asset_key in ("receivables", "inventory", "other_current_assets"):
        now = _ratio(current.get(asset_key), current.get("revenue"))
        before = _ratio(prior.get(asset_key), prior.get("revenue"))
        if now is not None and before is not None and now > before:
            score += 1

    # Slower depreciation on the same asset base lifts reported profit.
    depreciation_now = _ratio(current.get("depreciation"), current.get("gross_ppe"))
    depreciation_before = _ratio(prior.get("depreciation"), prior.get("gross_ppe"))
    if depreciation_now is not None and depreciation_before is not None and depreciation_now < depreciation_before:
        score += 1

    growth = _ratio(current.get("total_assets"), prior.get("total_assets"))
    if growth is not None:
        # Rounded before comparing: 1100/1000 yields 10.000000000000009, which
        # would trip an exclusive threshold at exactly 10%.
        if round((growth - 1.0) * 100.0, 6) > _ASSET_GROWTH_FLAG_PCT:
            score += 1

    return score
