"""Detector A.2a: Altman Z''-Score (bankruptcy distress).

Uses the Z''-Score (general form, not industry-specific). Z'' applies
to manufacturers, non-manufacturers, public, and private alike — chosen
for portability since most Discovery tickers aren't pure manufacturers
and Z (original) misclassifies tech/services as distressed.

Z'' = 6.56*(WC/TA) + 3.26*(RE/TA) + 6.72*(EBIT/TA) + 1.05*(BVE/TL)

Tiers (Altman 1968/2000 calibration):
  - Z'' > 2.6           : Safe Zone     -> no flag
  - 1.1 <= Z'' <= 2.6   : Grey Zone     -> INFO (25)
  - Z'' < 1.1           : Distress Zone -> CRITICAL (60)

Excluded sectors:
  - Financial Services / Insurance: Z-Score does not apply to banks
    (different balance-sheet structure: WC/TA and EBIT/TA aren't
    meaningful when float deposits dominate liabilities).
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
_CURRENT_LIABILITIES = ("Current Liabilities", "Total Current Liabilities")
_RETAINED_EARNINGS = ("Retained Earnings",)
_EBIT = ("EBIT", "Operating Income")
_TOTAL_LIABILITIES = (
    "Total Liabilities Net Minority Interest",
    "Total Liab",
    "Total Liabilities",
)
_STOCKHOLDERS_EQUITY = (
    "Stockholders Equity",
    "Total Stockholder Equity",
    "Common Stock Equity",
)

_FINANCIAL_SECTORS = {"Financial Services", "Financial", "Insurance"}

_SAFE_ZONE = 2.6
_GREY_ZONE_FLOOR = 1.1


async def detect_altman_z_score(ticker: str) -> list[RedFlagFinding]:
    """Return at most one finding. Empty on missing data, financial-sector
    ticker, or any yfinance failure.
    """
    sym = ticker.strip().upper()
    bundle = await get_forensic_bundle(sym)
    if bundle is None:
        return []
    if bundle.sector and bundle.sector in _FINANCIAL_SECTORS:
        return []

    bs = bundle.balance_sheet
    is_ = bundle.income_statement
    if bs.empty or is_.empty:
        return []

    ta = safe_row(bs, _TOTAL_ASSETS, 0)
    ca = safe_row(bs, _CURRENT_ASSETS, 0)
    cl = safe_row(bs, _CURRENT_LIABILITIES, 0)
    re = safe_row(bs, _RETAINED_EARNINGS, 0)
    ebit = safe_row(is_, _EBIT, 0)
    tl = safe_row(bs, _TOTAL_LIABILITIES, 0)
    bve = safe_row(bs, _STOCKHOLDERS_EQUITY, 0)

    if ta is None or ta <= 0 or tl is None or tl <= 0:
        return []
    if any(v is None for v in (ca, cl, re, ebit, bve)):
        return []
    # Type narrowing for the math below
    assert ca is not None and cl is not None and re is not None and ebit is not None and bve is not None

    wc = ca - cl
    z = (
        6.56 * (wc / ta)
        + 3.26 * (re / ta)
        + 6.72 * (ebit / ta)
        + 1.05 * (bve / tl)
    )

    if z > _SAFE_ZONE:
        return []

    if z < _GREY_ZONE_FLOOR:
        severity = Severity.CRITICAL
        score = 60.0
        zone = "Distress Zone"
    else:
        severity = Severity.INFO
        score = 25.0
        zone = "Grey Zone"

    headline = f"Altman Z''={z:.2f} ({zone}) - bankruptcy risk indicator"

    return [RedFlagFinding(
        detector="altman_z_score",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "z_score": round(z, 2),
            "zone": zone,
            "components": {
                "wc_over_ta": round(wc / ta, 4),
                "re_over_ta": round(re / ta, 4),
                "ebit_over_ta": round(ebit / ta, 4),
                "bve_over_tl": round(bve / tl, 4),
            },
            "sector": bundle.sector,
        },
    )]
