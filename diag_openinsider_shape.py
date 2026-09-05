"""Final OpenInsider diag: count ticker frequency and insider repetition
in the actual records. Tells us whether the source thresholds are too
strict OR whether OpenInsider's data really is one-row-per-ticker.
"""

import asyncio
import logging
from collections import Counter, defaultdict

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING)


async def analyze(screener: str) -> None:
    print(f"\n=== {screener} ===")
    from app.backend.services.openinsider_service import get_openinsider_screener
    r = await get_openinsider_screener(screener, None)
    if r is None or not r.records:
        print("  no records")
        return

    print(f"  total records: {len(r.records)}")
    ticker_counts = Counter(rec.ticker.upper() for rec in r.records if rec.ticker)
    print(f"  unique tickers: {len(ticker_counts)}")
    print(f"  tickers with 3+ records: {sum(1 for n in ticker_counts.values() if n >= 3)}")
    print(f"  tickers with 5+ records: {sum(1 for n in ticker_counts.values() if n >= 5)}")
    print(f"  top-5 most-frequent tickers: {ticker_counts.most_common(5)}")

    by_ticker_insiders: dict[str, set] = defaultdict(set)
    for rec in r.records:
        if rec.ticker and rec.insider_name:
            by_ticker_insiders[rec.ticker.upper()].add(rec.insider_name)
    multi_insider = {t: len(s) for t, s in by_ticker_insiders.items() if len(s) >= 3}
    print(f"  tickers with 3+ DISTINCT insiders: {len(multi_insider)}")
    if multi_insider:
        print(f"  examples: {dict(list(multi_insider.items())[:5])}")

    sample_dates = {rec.trade_date for rec in r.records[:10] if rec.trade_date}
    print(f"  sample trade_date values: {list(sample_dates)[:5]}")


async def main() -> None:
    print("=" * 60)
    print("OPENINSIDER RECORD-SHAPE ANALYSIS")
    print("=" * 60)
    for s in ["cluster_buy", "latest_insider_buys_25k", "ceo_cfo_conviction"]:
        await analyze(s)


if __name__ == "__main__":
    asyncio.run(main())
