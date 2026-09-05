"""Probe the two still-silent EDGAR sources to isolate WHY they return 0.

Either:
  - Empty input universe (no spinoff CIKs in DB → csuite_buy returns [] before EDGAR)
  - EDGAR returns 0 rows for the search (need to widen lookback / form list)
  - EDGAR raises but error is swallowed somewhere

Run: poetry run python diag_edgar.py
"""

import asyncio
import logging
import os
import traceback

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(name)s :: %(message)s")


def show_edgar_identity() -> None:
    print("--- EDGAR_IDENTITY ---")
    val = os.environ.get("EDGAR_IDENTITY", "")
    print(f"  raw value: {val!r}")
    print(f"  has space (name + email pattern): {' ' in val}")
    print(f"  has @ (email present): {'@' in val}")
    print()


def show_spinoff_universe() -> None:
    print("--- csuite_buy input universe (spinoff_filings table) ---")
    from app.backend.database import SessionLocal
    from app.backend.database.models import SpinoffFiling
    db = SessionLocal()
    try:
        total = db.query(SpinoffFiling).count()
        sample = db.query(SpinoffFiling.cik, SpinoffFiling.filing_date).limit(5).all()
        print(f"  total spinoff rows: {total}")
        print(f"  sample: {sample}")
    finally:
        db.close()
    print()


async def probe_csuite_buy() -> None:
    print("--- csuite_buy fetch() ---")
    from app.backend.services.discovery_service._sources import csuite_buy
    try:
        result = await csuite_buy.fetch()
    except Exception as exc:
        print(f"  RAISED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return
    print(f"  returned {len(result)} items")
    if result:
        print(f"  first: {result[0]!r}")
    print()


async def probe_activist_13d() -> None:
    print("--- activist_13d fetch() ---")
    from app.backend.services.discovery_service._sources import activist_13d
    # Bypass the 6h cache for this diagnostic — same poke pattern as cache_service._flush_activist_13d_module
    activist_13d.__dict__["_cache"] = None
    activist_13d.__dict__["_cache_ts"] = 0.0
    try:
        result = await activist_13d.fetch()
    except Exception as exc:
        print(f"  RAISED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return
    print(f"  returned {len(result)} items")
    if result:
        print(f"  first: {result[0]!r}")
    print()


async def main() -> None:
    show_edgar_identity()
    show_spinoff_universe()
    await probe_csuite_buy()
    await probe_activist_13d()


if __name__ == "__main__":
    asyncio.run(main())
