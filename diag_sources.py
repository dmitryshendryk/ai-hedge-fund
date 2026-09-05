"""Per-source diagnostic: call each previously-silent Discovery source
directly with full logging, capture the raw return + any exception, and
report exactly why it emitted zero ideas.

Run: poetry run python diag_sources.py
"""

import asyncio
import importlib
import logging
import os
import traceback

from dotenv import load_dotenv

# Backend loads .env via dotenv at startup; standalone script must do the
# same or every env var reads as MISSING and we'd misdiagnose every source.
load_dotenv()

# Enable WARNING+ from all services so we see EDGAR/OpenInsider/Finnhub failures.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)

# The 6 sources that returned 0 in your last run plus commodity_tailwind.
SILENT_SOURCES = [
    "csuite_buy",
    "cluster_buy",
    "repeat_buyer",
    "analyst",
    "activist_13d",
    "contrarian_setup",
    "commodity_tailwind",
]


def report_env() -> None:
    """Surface env vars that EDGAR / OpenInsider / Finnhub / FRED need.
    A missing EDGAR_IDENTITY is the #1 cause of 403 Forbidden from SEC.
    """
    print("=" * 70)
    print("ENVIRONMENT CHECK")
    print("=" * 70)
    keys = [
        "EDGAR_IDENTITY",
        "FINNHUB_API_KEY",
        "FRED_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ]
    for k in keys:
        v = os.environ.get(k, "")
        if not v:
            print(f"  {k:.<30} MISSING")
        else:
            shape = f"set (len={len(v)})"
            if k == "EDGAR_IDENTITY":
                # Full value matters: SEC validates the email format
                shape = f"set: {v!r}"
            print(f"  {k:.<30} {shape}")
    print()


async def diagnose_one(source_name: str) -> None:
    print("=" * 70)
    print(f"SOURCE: {source_name}")
    print("=" * 70)
    try:
        mod = importlib.import_module(
            f"app.backend.services.discovery_service._sources.{source_name}"
        )
    except Exception as exc:
        print(f"  IMPORT FAILED: {exc!r}")
        traceback.print_exc()
        return

    fetch_fn = mod.__dict__.get("fetch")
    if fetch_fn is None or not callable(fetch_fn):
        print("  NO fetch() FOUND")
        return

    try:
        result = await fetch_fn()
    except Exception as exc:
        print(f"  FETCH RAISED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    if result is None:
        print("  FETCH returned None")
        return
    if not isinstance(result, list):
        print(f"  FETCH returned non-list: {type(result).__name__}")
        return

    print(f"  FETCH returned {len(result)} item(s)")
    if result:
        first = result[0]
        print(f"  First item: {first!r}")


async def main() -> None:
    report_env()
    for src in SILENT_SOURCES:
        try:
            await diagnose_one(src)
        except Exception as exc:
            print(f"  UNEXPECTED ERROR while diagnosing {src}: {exc!r}")
            traceback.print_exc()
        print()


if __name__ == "__main__":
    asyncio.run(main())
