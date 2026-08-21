"""Advanced fundamental metrics: valuation, cash return, solvency, and
accounting-manipulation scoring.

Pure arithmetic only — callers supply line items they already fetched. Every
function returns None when an input is missing or the result would be
economically meaningless, because a fabricated ratio reads as real evidence in a
screen or a red-flag badge.
"""

import math

# Montier C-Score flag: balance-sheet growth beyond this rate reads as a
# manipulation signal rather than ordinary expansion.
_ASSET_GROWTH_FLAG_PCT: float = 10.0


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
