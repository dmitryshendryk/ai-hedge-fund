"""insider_dumping exit-alert rule.

Fires when 3+ distinct insiders sell within OpenInsider's 90-day cluster_sell
window AND at least one of them dumped ≥20% of their pre-trade holdings.
Scoped to watchlist tickers — exit signals should hit names the user owns,
not the broader market.

Severity:
  - warning: 3-4 insiders, max-dump 20-50%
  - critical: 5+ insiders OR any insider dumped ≥50% of their stake
"""

import logging
from collections import defaultdict

from app.backend.services.alert_service._rules._helpers import watchlist_ticker_set
from app.backend.services.alert_service._types import AlertCandidate

logger = logging.getLogger(__name__)

_MIN_INSIDERS = 3
_DUMP_RATIO_MIN = 0.20
_DUMP_RATIO_CRITICAL = 0.50
_BIG_CLUSTER = 5


def _dump_ratio(qty: int | None, owned: int | None) -> float | None:
    if qty is None or owned is None:
        return None
    sold = abs(qty)
    pre_trade = owned + sold
    if pre_trade <= 0:
        return None
    return sold / pre_trade


async def evaluate(_thresholds: dict) -> list[AlertCandidate]:
    from app.backend.services.openinsider_service import get_openinsider_screener

    watchlist = watchlist_ticker_set()
    if not watchlist:
        return []

    try:
        response = await get_openinsider_screener("cluster_sell", None)
    except Exception as exc:
        logger.warning("insider_dumping: source fetch failed: %s", exc)
        return []

    by_ticker: dict[str, list] = defaultdict(list)
    for rec in response.records:
        if not rec.ticker:
            continue
        ticker = rec.ticker.upper()
        if ticker not in watchlist:
            continue
        by_ticker[ticker].append(rec)

    out: list[AlertCandidate] = []
    for ticker, recs in by_ticker.items():
        distinct_insiders = len({r.insider_name for r in recs if r.insider_name})
        if distinct_insiders < _MIN_INSIDERS:
            continue

        dump_ratios = [_dump_ratio(r.qty, r.owned) for r in recs]
        valid_ratios = [r for r in dump_ratios if r is not None]
        max_dump = max(valid_ratios) if valid_ratios else 0.0
        if max_dump < _DUMP_RATIO_MIN:
            continue

        total_value = sum(r.value or 0 for r in recs)
        names = sorted({r.insider_name for r in recs if r.insider_name})
        names_preview = ", ".join(names[:5])
        if len(names) > 5:
            names_preview += f" (+{len(names) - 5} more)"

        is_critical = distinct_insiders >= _BIG_CLUSTER or max_dump >= _DUMP_RATIO_CRITICAL
        severity = "critical" if is_critical else "warning"

        max_dump_pct = max_dump * 100
        dates = [r.trade_date for r in recs if r.trade_date]
        first_date = min(dates) if dates else None
        last_date = max(dates) if dates else None

        out.append(AlertCandidate(
            rule_type="insider_dumping",
            ticker=ticker[:20],
            title=f"🚪 Insider dumping: {ticker} ({distinct_insiders} insiders · max {max_dump_pct:.0f}% of stake)",
            message=(
                f"Insiders are exiting {ticker} aggressively.\n"
                f"{distinct_insiders} distinct insiders sold ${total_value:,.0f} total.\n"
                f"Largest dump: {max_dump_pct:.0f}% of one insider's pre-trade holdings.\n"
                + (f"Insiders: {names_preview}\n" if names_preview else "")
                + (f"Date range: {first_date} to {last_date}\n" if first_date else "")
            ),
            payload={
                "ticker": ticker,
                "distinct_insiders": distinct_insiders,
                "total_value": total_value,
                "max_dump_ratio": max_dump,
                "insider_names": names[:10],
                "first_date": first_date,
                "last_date": last_date,
            },
            severity=severity,
        ))
    return out
