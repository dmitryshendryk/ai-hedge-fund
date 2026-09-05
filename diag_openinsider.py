"""Direct probe of OpenInsider HTTP reachability + parser.

Tells us if the issue is:
  - Network: can't even reach openinsider.com
  - Block: reachable but returns 403/empty
  - Parse: returns HTML but parser sees 0 rows

Run: poetry run python diag_openinsider.py
"""

import asyncio
import logging
import traceback

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)


async def probe_one(screener: str) -> None:
    print(f"--- screener: {screener} ---")
    try:
        from app.backend.models.openinsider_schemas import OpenInsiderResponse
        from app.backend.services.openinsider_service import get_openinsider_screener
        response = await get_openinsider_screener(screener, None)
    except Exception as exc:
        print(f"  RAISED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    if response is None:
        print("  response is None")
        return
    if not isinstance(response, OpenInsiderResponse):
        print(f"  unexpected type: {type(response).__name__}")
        return

    print(f"  records: {len(response.records)}  (total={response.total}, cached={response.cached})")
    if response.records:
        first = response.records[0]
        print(f"  first: ticker={first.ticker}  insider={first.insider_name!r}  "
              f"title={first.title!r}  qty={first.qty}  value={first.value}  "
              f"trade_type={first.trade_type!r}  filing_date={first.filing_date}")


async def main() -> None:
    print("=" * 60)
    print("OPENINSIDER REACHABILITY PROBE")
    print("=" * 60)
    for s in ["cluster_buy", "latest_insider_buys_25k", "ceo_cfo_conviction"]:
        await probe_one(s)
        print()


if __name__ == "__main__":
    asyncio.run(main())
