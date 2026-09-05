"""Discovery source: high-quality fund initiates a NEW 13F position.

Reads the user-curated WhaleFund list (Berkshire, Pershing Square, Baupost,
Tiger, etc.), fetches each whale's most-recent 13F-HR filing via edgartools,
and emits an IdeaSignal for every ticker where at least one whale opened a
NEW position above the size threshold in the latest quarter.

This source brings the whale-tracking signal into Discovery's candidate
generation. Other catalyst sources read insider Form 4s and SEC events;
this one reads the institutional positioning of curated smart-money funds,
which surfaces names that no other Camp A source covers.

13F filings are quarterly and lagged ~45 days, so output is stable within
a quarter and refreshes when fresh filings hit EDGAR. Reuses the existing
public `get_aggregate_holdings` helper, which already handles parallel CIK
fetches via a thread pool.

Score:
  - +30 base:  whale opened NEW position ≥ $50M
  - +40:       NEW position ≥ $200M (sized conviction)
  - +50:       NEW position ≥ $500M (high-conviction whale bet)
  - +10 bonus when 2+ whales opened the same NEW position same quarter
"""

import logging

from app.backend.database import SessionLocal
from app.backend.database.models import WhaleFund
from app.backend.models.discovery_schemas import IdeaSignal

logger = logging.getLogger(__name__)

_NEW_STATUS = "NEW"
_MIN_NEW_VALUE = 50_000_000.0
_SIZED_VALUE = 200_000_000.0
_HIGH_CONVICTION_VALUE = 500_000_000.0
_CLUSTER_BONUS = 10.0


async def fetch() -> list[tuple[str, IdeaSignal]]:
    from app.backend.services.insider_service import get_aggregate_holdings

    db = SessionLocal()
    try:
        whale_rows = db.query(WhaleFund.cik, WhaleFund.name).all()
    finally:
        db.close()

    if not whale_rows:
        return []

    cik_list = [int(cik) for cik, _ in whale_rows]
    cik_to_name: dict[int, str] = {int(cik): name for cik, name in whale_rows}

    try:
        agg = await get_aggregate_holdings(cik_list)
    except Exception as exc:
        logger.warning("thirteenf_new_buy: aggregate fetch failed: %s", exc)
        return []

    out: list[tuple[str, IdeaSignal]] = []
    for record in agg.records:
        new_buyers = [
            cd for cd in record.company_details
            if cd.status == _NEW_STATUS and (cd.value or 0) >= _MIN_NEW_VALUE
        ]
        if not new_buyers:
            continue

        max_new_value = max((cd.value or 0) for cd in new_buyers)
        if max_new_value >= _HIGH_CONVICTION_VALUE:
            base = 50.0
            tier = "high-conviction whale buy"
        elif max_new_value >= _SIZED_VALUE:
            base = 40.0
            tier = "sized whale entry"
        else:
            base = 30.0
            tier = "whale initiation"

        score = base + (_CLUSTER_BONUS if len(new_buyers) >= 2 else 0.0)

        whale_names = sorted({cik_to_name.get(cd.cik, f"CIK {cd.cik}") for cd in new_buyers})
        display = ", ".join(whale_names[:3])
        if len(whale_names) > 3:
            display += f" +{len(whale_names) - 3}"
        label = f"{tier}: {display} opened ${max_new_value / 1e6:.0f}M"

        out.append((record.ticker, IdeaSignal(
            source="thirteenf_new_buy",
            score=score,
            label=label,
            detail={
                "ticker": record.ticker,
                "issuer": record.issuer,
                "new_buyer_count": len(new_buyers),
                "max_new_value": max_new_value,
                "whales": [
                    {
                        "company": cik_to_name.get(cd.cik, f"CIK {cd.cik}"),
                        "cik": cd.cik,
                        "shares": cd.shares,
                        "value": cd.value,
                    }
                    for cd in new_buyers
                ],
                "tier": tier,
            },
        )))
    return out
