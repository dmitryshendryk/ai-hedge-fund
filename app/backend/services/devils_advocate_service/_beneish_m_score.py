"""Detector A.2b: Beneish M-Score (earnings manipulation probability).

M = -4.84 + 0.92*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI + 0.115*DEPI
    - 0.172*SGAI + 4.679*TATA - 0.327*LVGI

Where each ratio compares year t to year t-1:
  DSRI : Days Sales in Receivables Index
  GMI  : Gross Margin Index   (note: prior / current — direction matters)
  AQI  : Asset Quality Index
  SGI  : Sales Growth Index
  DEPI : Depreciation Index   (prior / current)
  SGAI : SG&A-over-Sales Index
  TATA : Total Accruals to Total Assets (year t only)
  LVGI : Leverage Index

Threshold (Beneish 1999 — famously caught Enron pre-collapse):
  - M > -1.78           : Likely manipulator -> CRITICAL (60)
  - -2.22 < M <= -1.78  : Borderline grey    -> INFO (25)
  - M <= -2.22          : Likely clean       -> no flag

Requires 2 full years of statements. Excludes Financial Services
(different accruals model — banks accrue loan loss provisions, REITs
have non-cash depreciation models that the formula reads as red flags).
"""
import logging

from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.devils_advocate_service._yfinance_fundamentals import (
    get_forensic_bundle,
    safe_row,
)

logger = logging.getLogger(__name__)

_TOTAL_ASSETS = ("Total Assets",)
_CURRENT_ASSETS = ("Current Assets", "Total Current Assets")
_RECEIVABLES = ("Net Receivables", "Receivables", "Accounts Receivable")
_PPE = (
    "Net PPE",
    "Property Plant Equipment Net",
    "Net Property Plant And Equipment",
)
_TOTAL_LIABILITIES = (
    "Total Liabilities Net Minority Interest",
    "Total Liab",
    "Total Liabilities",
)
_TOTAL_REVENUE = ("Total Revenue", "Revenue")
_COST_OF_REVENUE = ("Cost Of Revenue", "Reconciled Cost Of Revenue")
_SGA = (
    "Selling General And Administration",
    "Selling General Administrative",
)
_NET_INCOME = ("Net Income", "Net Income Common Stockholders")
_OPERATING_CASH_FLOW = (
    "Operating Cash Flow",
    "Cash Flow From Continuing Operating Activities",
)
_DEPRECIATION = (
    "Depreciation And Amortization",
    "Depreciation",
)

_FINANCIAL_SECTORS = {"Financial Services", "Financial", "Insurance"}

_MANIPULATOR_THRESHOLD = -1.78
_GREY_THRESHOLD = -2.22


def _safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


async def detect_beneish_m_score(ticker: str) -> list[RedFlagFinding]:
    """Return at most one finding. Empty when fewer than 2 years of
    statements, missing line items, financial-sector ticker, or any
    yfinance failure.
    """
    sym = ticker.strip().upper()
    bundle = await get_forensic_bundle(sym)
    if bundle is None:
        return []
    if bundle.sector and bundle.sector in _FINANCIAL_SECTORS:
        return []

    bs = bundle.balance_sheet
    is_ = bundle.income_statement
    cf = bundle.cash_flow
    if bs.empty or is_.empty or cf.empty:
        return []
    if len(bs.columns) < 2 or len(is_.columns) < 2 or len(cf.columns) < 2:
        return []

    sales_t = safe_row(is_, _TOTAL_REVENUE, 0)
    sales_p = safe_row(is_, _TOTAL_REVENUE, 1)
    cogs_t = safe_row(is_, _COST_OF_REVENUE, 0)
    cogs_p = safe_row(is_, _COST_OF_REVENUE, 1)
    sga_t = safe_row(is_, _SGA, 0)
    sga_p = safe_row(is_, _SGA, 1)
    ni_t = safe_row(is_, _NET_INCOME, 0)
    cfo_t = safe_row(cf, _OPERATING_CASH_FLOW, 0)
    dep_t = safe_row(cf, _DEPRECIATION, 0)
    dep_p = safe_row(cf, _DEPRECIATION, 1)

    ar_t = safe_row(bs, _RECEIVABLES, 0)
    ar_p = safe_row(bs, _RECEIVABLES, 1)
    ta_t = safe_row(bs, _TOTAL_ASSETS, 0)
    ta_p = safe_row(bs, _TOTAL_ASSETS, 1)
    ca_t = safe_row(bs, _CURRENT_ASSETS, 0)
    ca_p = safe_row(bs, _CURRENT_ASSETS, 1)
    ppe_t = safe_row(bs, _PPE, 0)
    ppe_p = safe_row(bs, _PPE, 1)
    tl_t = safe_row(bs, _TOTAL_LIABILITIES, 0)
    tl_p = safe_row(bs, _TOTAL_LIABILITIES, 1)

    required = (
        sales_t, sales_p, cogs_t, cogs_p, sga_t, sga_p, ni_t, cfo_t,
        dep_t, dep_p, ar_t, ar_p, ta_t, ta_p, ca_t, ca_p,
        ppe_t, ppe_p, tl_t, tl_p,
    )
    if any(v is None for v in required):
        return []
    assert (
        sales_t is not None and sales_p is not None and cogs_t is not None and cogs_p is not None
        and sga_t is not None and sga_p is not None and ni_t is not None and cfo_t is not None
        and dep_t is not None and dep_p is not None and ar_t is not None and ar_p is not None
        and ta_t is not None and ta_p is not None and ca_t is not None and ca_p is not None
        and ppe_t is not None and ppe_p is not None and tl_t is not None and tl_p is not None
    )

    if sales_t <= 0 or sales_p <= 0 or ta_t <= 0 or ta_p <= 0:
        return []

    dsri = _safe_div(_safe_div(ar_t, sales_t), _safe_div(ar_p, sales_p))

    gm_t = (sales_t - cogs_t) / sales_t
    gm_p = (sales_p - cogs_p) / sales_p
    if gm_t == 0:
        return []
    gmi = gm_p / gm_t

    aq_t = 1.0 - (ca_t + ppe_t) / ta_t
    aq_p = 1.0 - (ca_p + ppe_p) / ta_p
    if aq_p == 0:
        return []
    aqi = aq_t / aq_p

    sgi = sales_t / sales_p

    dep_p_sum = dep_p + ppe_p
    dep_t_sum = dep_t + ppe_t
    if dep_p_sum == 0 or dep_t_sum == 0:
        return []
    depi = _safe_div(dep_p / dep_p_sum, dep_t / dep_t_sum)

    sgai = _safe_div(sga_t / sales_t, sga_p / sales_p)

    tata = (ni_t - cfo_t) / ta_t

    lvgi = _safe_div(tl_t / ta_t, tl_p / ta_p)

    if any(c is None for c in (dsri, depi, sgai, lvgi)):
        return []
    assert dsri is not None and depi is not None and sgai is not None and lvgi is not None

    m = (
        -4.84
        + 0.92 * dsri
        + 0.528 * gmi
        + 0.404 * aqi
        + 0.892 * sgi
        + 0.115 * depi
        - 0.172 * sgai
        + 4.679 * tata
        - 0.327 * lvgi
    )

    if m <= _GREY_THRESHOLD:
        return []

    if m > _MANIPULATOR_THRESHOLD:
        severity = Severity.CRITICAL
        score = 60.0
        verdict = "Likely earnings manipulation"
    else:
        severity = Severity.INFO
        score = 25.0
        verdict = "Borderline (grey zone)"

    headline = f"Beneish M={m:.2f} - {verdict}"

    return [RedFlagFinding(
        detector="beneish_m_score",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "m_score": round(m, 2),
            "verdict": verdict,
            "components": {
                "dsri": round(dsri, 3),
                "gmi": round(gmi, 3),
                "aqi": round(aqi, 3),
                "sgi": round(sgi, 3),
                "depi": round(depi, 3),
                "sgai": round(sgai, 3),
                "tata": round(tata, 4),
                "lvgi": round(lvgi, 3),
            },
            "sector": bundle.sector,
        },
    )]
