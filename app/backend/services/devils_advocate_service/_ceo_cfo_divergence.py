"""Detector A.1: CEO/CFO insider trading divergence.

Bear thesis: when CEO and CFO trade Form 4 in *opposite* directions over
the same recent window, it's a sign of internal disagreement about the
near-term outlook. CFOs sell when they see the model break before it
shows in the numbers; if the CEO is meanwhile accumulating, it warrants
a second look before treating the ticker as a clean Discovery long.

Read-only against `insider_service.get_ownership_changes(ticker, "4")`.
We do NOT mutate Discovery state, ranking, or scores.

Score tiers (per ticker, only ever ONE finding from this detector):
  - 30 INFO: any directional disagreement (CEO buying / CFO selling, or
              the reverse) within the lookback window
  - 45 WARNING: disagreement AND the dollar/share net move is material
                (CFO net-sells > 10k shares OR > 5x CEO position)
  - 60 CRITICAL: disagreement AND CFO net-sell is dominant (> 50k shares
                 or > 25x the CEO buy size)

Material-move thresholds are intentionally share-count based: we don't
have a reliable per-row value in `OwnershipChangeRecord`, so we stay
honest about what the data supports.
"""
import logging
from datetime import date, datetime, timedelta

from app.backend.models.insider_schemas import OwnershipChangeRecord
from app.backend.services.devils_advocate_service._schemas import RedFlagFinding, Severity
from app.backend.services.insider_service import get_ownership_changes

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 180
_FORM_TYPE = "4"

_CEO_TOKENS = ("ceo", "chief executive")
_CFO_TOKENS = ("cfo", "chief financial")

_MIN_NET_SHARES_FOR_SIGNAL = 100  # below this we treat the trade as too small to count

_MATERIAL_CFO_NET_SELL_SHARES = 10_000
_MATERIAL_CFO_RATIO = 5.0
_DOMINANT_CFO_NET_SELL_SHARES = 50_000
_DOMINANT_CFO_RATIO = 25.0


def _matches(position: str, tokens: tuple[str, ...]) -> bool:
    pos = (position or "").lower()
    return any(tok in pos for tok in tokens)


def _within_lookback(record_date: str, cutoff: date) -> bool:
    try:
        d = datetime.strptime(record_date[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return d >= cutoff


def _net_for_role(records: list[OwnershipChangeRecord], tokens: tuple[str, ...], cutoff: date) -> int:
    total = 0
    for r in records:
        if not _within_lookback(r.filing_date, cutoff):
            continue
        if not _matches(r.position, tokens):
            continue
        total += r.net_change
    return total


async def detect_ceo_cfo_divergence(ticker: str) -> list[RedFlagFinding]:
    """Return at most one finding. Empty on no disagreement, insufficient
    data, or any failure inside the insider service — failures must not
    break the parent report.
    """
    sym = ticker.strip().upper()
    try:
        ownership = await get_ownership_changes(sym, form_type=_FORM_TYPE, limit=200, offset=0)
    except Exception as exc:
        logger.warning("devils_advocate: ownership fetch failed for %s: %s", sym, exc)
        return []

    cutoff = datetime.utcnow().date() - timedelta(days=_LOOKBACK_DAYS)
    ceo_net = _net_for_role(ownership.records, _CEO_TOKENS, cutoff)
    cfo_net = _net_for_role(ownership.records, _CFO_TOKENS, cutoff)

    if abs(ceo_net) < _MIN_NET_SHARES_FOR_SIGNAL or abs(cfo_net) < _MIN_NET_SHARES_FOR_SIGNAL:
        return []
    if (ceo_net > 0) == (cfo_net > 0):
        return []

    cfo_sell_dominant = cfo_net < 0 and ceo_net > 0
    cfo_buy_dominant = cfo_net > 0 and ceo_net < 0

    cfo_abs = abs(cfo_net)
    ceo_abs = abs(ceo_net) or 1
    ratio = cfo_abs / ceo_abs

    if cfo_sell_dominant and (cfo_abs >= _DOMINANT_CFO_NET_SELL_SHARES or ratio >= _DOMINANT_CFO_RATIO):
        severity = Severity.CRITICAL
        score = 60.0
        headline = f"CRITICAL: CEO buying {ceo_net:+,} sh while CFO dumping {cfo_net:+,} sh ({ratio:.1f}x)"
    elif cfo_sell_dominant and (cfo_abs >= _MATERIAL_CFO_NET_SELL_SHARES or ratio >= _MATERIAL_CFO_RATIO):
        severity = Severity.WARNING
        score = 45.0
        headline = f"WARNING: CEO buying {ceo_net:+,} sh while CFO selling {cfo_net:+,} sh"
    elif cfo_buy_dominant:
        severity = Severity.INFO
        score = 30.0
        headline = f"NOTE: CFO accumulating {cfo_net:+,} sh while CEO selling {ceo_net:+,} sh"
    else:
        severity = Severity.INFO
        score = 30.0
        headline = f"NOTE: directional disagreement (CEO {ceo_net:+,} / CFO {cfo_net:+,})"

    return [RedFlagFinding(
        detector="ceo_cfo_divergence",
        score=score,
        severity=severity,
        headline=headline,
        detail={
            "ticker": sym,
            "lookback_days": _LOOKBACK_DAYS,
            "ceo_net_shares": ceo_net,
            "cfo_net_shares": cfo_net,
            "ratio_cfo_to_ceo": round(ratio, 2),
        },
    )]
