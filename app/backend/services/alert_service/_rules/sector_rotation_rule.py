"""sector_rotation_divergence alert rule (market-wide).

Detects money rotating OUT of semiconductors and INTO small caps: the
Semiconductor ETF (SMH) falling while the small-cap ETF (IWM) rises over the
same short window. This is the macro "tide" that drags high-beta semi
holdings down regardless of company fundamentals.

Market-wide, so it has no single ticker — emits under the synthetic ticker
"_MARKET_". The alert sink de-dupes on (rule_type, ticker, created_at), so a
multi-day episode surfaces once per scan window rather than per ticker.
"""
import logging
from datetime import date, timedelta

from app.backend.services.alert_service._types import AlertCandidate
from app.backend.services.pricing_service import compute_alpha

logger = logging.getLogger(__name__)

_MARKET_TICKER = "_MARKET_"
_SEMI_ETF = "SMH"
_SMALLCAP_ETF = "IWM"

# yfinance reports semiconductors under the "Technology" sector. Held names in
# these sectors are the ones a semi/high-beta rotation drags down first.
_AT_RISK_SECTORS = {"technology"}


async def _at_risk_holdings() -> list[str]:
    """Held tickers in a high-beta sector — the names most exposed to the
    rotation. Best-effort: any failure (no positions table, yfinance down)
    returns [] so the alert still fires with its generic body.
    """
    try:
        from app.backend.database import SessionLocal
        from app.backend.database.models import Position
        from app.backend.services.fundamentals_service import get_company_metrics_batch

        db = SessionLocal()
        try:
            tickers = [row[0].upper() for row in db.query(Position.ticker).all() if row[0]]
        finally:
            db.close()
        if not tickers:
            return []

        metrics_by_ticker = await get_company_metrics_batch(tickers)
        at_risk = [
            t for t in tickers
            if (getattr(metrics_by_ticker.get(t), "sector", None) or "").strip().lower() in _AT_RISK_SECTORS
        ]
        return sorted(at_risk)
    except Exception as exc:
        logger.debug("sector_rotation: at-risk holdings lookup failed: %s", exc)
        return []


async def evaluate(thresholds: dict) -> list[AlertCandidate]:
    smh_max = float(thresholds.get("smh_max_pct", -2.0))
    iwm_min = float(thresholds.get("iwm_min_pct", 1.0))
    lookback = int(thresholds.get("lookback_days", 3))
    since = date.today() - timedelta(days=lookback)

    smh = await compute_alpha(_SEMI_ETF, since)
    iwm = await compute_alpha(_SMALLCAP_ETF, since)
    if smh is None or iwm is None:
        logger.debug("sector_rotation: pricing unavailable, skipping scan")
        return []

    smh_ret = smh.period_return_pct
    iwm_ret = iwm.period_return_pct
    if not (smh_ret < smh_max and iwm_ret > iwm_min):
        return []

    at_risk = await _at_risk_holdings()
    if at_risk:
        exit_line = "EXIT WATCH on your holdings: " + ", ".join(at_risk)
    else:
        exit_line = "High-beta semiconductor holdings are at elevated pullback risk - consider tightening stops."

    severity = "critical" if smh_ret <= (smh_max - 1.5) else "warning"
    title = f"Sector rotation: semis {smh_ret:.1f}% vs small-caps +{iwm_ret:.1f}%"
    message = (
        f"Money is rotating out of semiconductors and into small caps over the last "
        f"{lookback} trading days.\n"
        f"SMH (semis): {smh_ret:.1f}%\n"
        f"IWM (small caps): +{iwm_ret:.1f}%\n"
        f"{exit_line}"
    )
    return [AlertCandidate(
        rule_type="sector_rotation",
        ticker=_MARKET_TICKER,
        title=title,
        message=message,
        payload={
            "smh_return_pct": round(smh_ret, 2),
            "iwm_return_pct": round(iwm_ret, 2),
            "lookback_days": lookback,
            "at_risk_holdings": at_risk,
        },
        severity=severity,
    )]
