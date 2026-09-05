"""Discovery source: true shareholder yield (the total return-of-capital signal).

Dividend yield alone understates what a disciplined company hands back. A buyer
of its own stock and a retirer of its own debt both transfer value to the
remaining equity, and neither shows in the dividend line. Summing all three
routes finds the compounder that is quietly shrinking its claims.

Every component is netted against its matching inflow, because a company that
issues more stock than it repurchases has returned nothing by that route. A
gross reading would score a serial diluter as a capital returner — the exact
trap this metric exists to expose.

Statements come from the Devil's Advocate ForensicBundle, which caches annual
filings for 30 minutes and already carries market cap. This source therefore
adds no yfinance fetch when the overlay has run on the same ticker.

Score: +25 when total yield exceeds 8%.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot, WatchlistItem
from app.backend.models.discovery_schemas import IdeaSignal
from app.backend.services.devils_advocate_service._yfinance_fundamentals import (
    get_forensic_bundle,
    safe_row,
)
from app.backend.services.fundamentals_service._advanced import true_shareholder_yield

logger = logging.getLogger(__name__)

_MIN_YIELD_PCT = 8.0
_QUALIFYING_SCORE = 25.0

_UNIVERSE_LOOKBACK_HOURS = 48
_MAX_UNIVERSE_SIZE = 25

# yfinance labels the same cash-flow line differently across tickers and
# vintages, so each item needs several candidates. Gross rows only — a "Net ..."
# row would double-count the inflow this source subtracts explicitly.
_DIVIDENDS_PAID = (
    "Cash Dividends Paid",
    "Common Stock Dividend Paid",
    "Dividends Paid",
    "Payments Of Dividends",
)
_STOCK_REPURCHASE = (
    "Repurchase Of Capital Stock",
    "Repurchase Of Common Stock",
    "Common Stock Payments",
)
_STOCK_ISSUANCE = (
    "Issuance Of Capital Stock",
    "Issuance Of Common Stock",
    "Common Stock Issuance",
)
_DEBT_REPAYMENT = (
    "Repayment Of Debt",
    "Long Term Debt Payments",
    "Repayments Of Long Term Debt",
)
_DEBT_ISSUANCE = (
    "Issuance Of Debt",
    "Long Term Debt Issuance",
    "Issuance Of Long Term Debt",
)


def _gather_universe() -> list[str]:
    db = SessionLocal()
    try:
        watchlist = {row[0].upper() for row in db.query(WatchlistItem.ticker).all() if row[0]}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=_UNIVERSE_LOOKBACK_HOURS)
        snapshots = {
            row[0].upper()
            for row in (
                db.query(DiscoverySnapshot.ticker)
                .filter(DiscoverySnapshot.snapshot_at >= cutoff)
                .filter(DiscoverySnapshot.is_ticker == True)  # noqa: E712
                .group_by(DiscoverySnapshot.ticker)
                .order_by(func.max(DiscoverySnapshot.score).desc())
                .limit(_MAX_UNIVERSE_SIZE)
                .all()
            )
            if row[0]
        }
    finally:
        db.close()
    return sorted(watchlist | snapshots)[:_MAX_UNIVERSE_SIZE]


async def _yield_for(ticker: str):
    """Total shareholder yield for one ticker, or None when data is thin.

    Column index 0 is the latest fiscal year in every ForensicBundle statement.
    """
    bundle = await get_forensic_bundle(ticker)
    if bundle is None:
        return None
    cash_flow = bundle.cash_flow
    return true_shareholder_yield(
        market_cap=bundle.market_cap,
        dividends_paid=safe_row(cash_flow, _DIVIDENDS_PAID, 0),
        buybacks=safe_row(cash_flow, _STOCK_REPURCHASE, 0),
        stock_issuance=safe_row(cash_flow, _STOCK_ISSUANCE, 0),
        debt_repayment=safe_row(cash_flow, _DEBT_REPAYMENT, 0),
        debt_issuance=safe_row(cash_flow, _DEBT_ISSUANCE, 0),
    )


def _label(result) -> str:
    parts = [
        f"{name} {pct:.1f}%"
        for name, pct in (
            ("dividend", result.dividend_pct),
            ("buyback", result.buyback_pct),
            ("debt paydown", result.debt_paydown_pct),
        )
        if pct > 0
    ]
    return f"shareholder yield {result.total_pct:.1f}% ({', '.join(parts)})"


async def fetch() -> list[tuple[str, IdeaSignal]]:
    try:
        universe = _gather_universe()
        if not universe:
            return []

        results = await asyncio.gather(
            *(_yield_for(t) for t in universe), return_exceptions=True,
        )

        out: list[tuple[str, IdeaSignal]] = []
        for ticker, result in zip(universe, results, strict=True):
            if isinstance(result, BaseException):
                logger.debug("true_shareholder_yield: error for %s: %s", ticker, result)
                continue
            if result is None or result.total_pct <= _MIN_YIELD_PCT:
                continue

            out.append((ticker, IdeaSignal(
                source="true_shareholder_yield",
                score=_QUALIFYING_SCORE,
                label=_label(result),
                detail={
                    "ticker": ticker,
                    "total_yield_pct": result.total_pct,
                    "dividend_pct": result.dividend_pct,
                    "buyback_pct": result.buyback_pct,
                    "debt_paydown_pct": result.debt_paydown_pct,
                    "min_yield_pct": _MIN_YIELD_PCT,
                },
            )))
        return out
    except Exception as exc:
        # The engine gathers sources with return_exceptions=True, so a raise
        # would only be logged as a dead source. Degrade to no ideas instead.
        logger.warning("true_shareholder_yield: source failed: %s", exc)
        return []
