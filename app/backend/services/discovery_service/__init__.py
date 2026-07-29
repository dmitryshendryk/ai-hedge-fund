"""Discovery service public API — get_ideas() with 1h cache + snapshot history.

On fresh compute (cache miss):
  1. Persist a DiscoverySnapshot row per idea (enables historical score tracking).
  2. If any ticker has high-confluence (>= 4 distinct signal sources AND score
     >= 80), the high_confluence alert rule is triggered immediately —
     bypassing the 4h AlertScheduler.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.backend.database import SessionLocal
from app.backend.database.models import DiscoverySnapshot
from app.backend.models.discovery_schemas import (
    DiscoveryConcentration,
    DiscoveryHistoryResponse,
    DiscoveryMover,
    DiscoveryMoversResponse,
    DiscoveryResponse,
    DiscoverySnapshotItem,
    MacroRegimeSnapshot,
    SectorBreakdown,
)
from app.backend.services.discovery_service._engine import aggregate_ideas

logger = logging.getLogger(__name__)

_HIGH_CONFLUENCE_MIN_SOURCES: int = 4
_HIGH_CONFLUENCE_MIN_SCORE: float = 80.0
_CACHE_TTL_SECONDS: float = 3600.0  # 1 hour
_CONCENTRATION_SAMPLE_SIZE: int = 200  # sector HUD computed over top-N, stable across pages

# Holds the full ranked universe per cold compute. Snapshot dataclass packages
# ideas + concentration + regime so pagination slices are pure list ops.
@dataclass(frozen=True)
class _CachedUniverse:
    ideas: list  # list[DiscoveryIdea] — full ranked, un-enriched
    concentration: DiscoveryConcentration
    macro_regime: MacroRegimeSnapshot
    generated_at: str


# Single-entry cache (replaces the per-limit dict). One universe per hour.
_cache: dict[str, tuple[_CachedUniverse, float]] = {}
_CACHE_KEY = "discovery:full"

# Single-flight de-dup: when two requests arrive while a compute is in
# progress, the second one awaits the first task instead of starting a
# parallel fanout.
_inflight_refreshes: dict[str, asyncio.Task] = {}


@dataclass(frozen=True)
class _UniverseLookup:
    """Universe fetch result + cache-hit flag for paging response metadata."""
    universe: _CachedUniverse
    cached: bool


def _has_high_confluence(ideas: list) -> bool:
    """True if any idea has >= 4 distinct signal sources AND score >= 80."""
    for idea in ideas:
        distinct_sources = len({s.source for s in idea.signals})
        if distinct_sources >= _HIGH_CONFLUENCE_MIN_SOURCES and idea.score >= _HIGH_CONFLUENCE_MIN_SCORE:
            return True
    return False


def _trigger_high_confluence_alert_in_background() -> None:
    """Fire-and-forget the immediate high_confluence alert evaluation.

    Wrapped in a background task so a slow Telegram send doesn't block the
    Discovery API response. Errors are logged but never raised.
    """
    async def _runner() -> None:
        try:
            # Deferred import: alert_service imports from discovery_service in
            # the high_confluence rule body, so a top-level import would create
            # a circular dependency at module load time.
            from app.backend.services.alert_service import trigger_rule_immediately
            created = await trigger_rule_immediately("high_confluence")
            if created > 0:
                logger.info("high_confluence: %d immediate alert(s) created", created)
        except Exception as exc:
            logger.warning("high_confluence immediate trigger failed: %s", exc)

    try:
        asyncio.create_task(_runner())
    except RuntimeError:
        logger.debug("No event loop; skipping immediate high_confluence trigger")


def _persist_snapshots(ideas: list) -> None:
    """Persist one DiscoverySnapshot row per idea. Runs synchronously inside
    the cold-compute flow (NOT per page — would double-write snapshots).
    Best-effort — DB errors are logged, never raised.
    """
    db: Session = SessionLocal()
    try:
        for idea in ideas:
            distinct_sources = len({s.source for s in idea.signals})
            signals_json = [
                {"source": s.source, "score": s.score, "label": s.label}
                for s in idea.signals
            ]
            db.add(DiscoverySnapshot(
                ticker=idea.ticker[:20],
                cik=idea.cik,
                is_ticker=idea.is_ticker,
                company=idea.company,
                score=idea.score,
                distinct_sources=distinct_sources,
                signals=signals_json,
            ))
        db.commit()
    except Exception as exc:
        logger.warning("Discovery snapshot persistence failed: %s", exc)
        db.rollback()
    finally:
        db.close()


async def _enrich_top_with_alpha(ideas: list, max_enrich: int, days: int) -> None:
    """Mutate the first `max_enrich` ticker-keyed ideas in place with N-day
    return %, SPY-relative alpha, distance_from_whale_entry_pct, and a
    company-name fallback when no contributing signal supplied one.

    The company-name lookup piggybacks on `get_company_metrics_batch`, which
    most of the quality sources have already populated for these tickers.
    Within the 24h fundamentals cache the call is essentially free.
    """
    from datetime import date, timedelta

    from app.backend.services.fundamentals_service import get_company_metrics_batch
    from app.backend.services.pricing_service import compute_alpha_batch
    from app.backend.services.whale_entry_service import get_distance_batch

    since = date.today() - timedelta(days=days)
    targets = [i for i in ideas[:max_enrich] if i.is_ticker and i.ticker]
    if not targets:
        return

    pairs: list[tuple[str, date]] = [(i.ticker, since) for i in targets]
    ticker_symbols = [i.ticker for i in targets]
    metrics_by_ticker, whale_dist_by_ticker, company_metrics_by_ticker = await asyncio.gather(
        compute_alpha_batch(pairs),
        get_distance_batch(ticker_symbols),
        get_company_metrics_batch(ticker_symbols),
    )
    for idea in targets:
        ticker_upper = idea.ticker.upper()
        m = metrics_by_ticker.get(ticker_upper)
        if m is not None:
            idea.return_30d_pct = m.period_return_pct
            idea.alpha_30d_pct = m.alpha_pct
        idea.distance_from_whale_entry_pct = whale_dist_by_ticker.get(ticker_upper)

        cm = company_metrics_by_ticker.get(ticker_upper)
        if cm is not None:
            if not idea.company and cm.long_name:
                idea.company = cm.long_name
            if cm.sector:
                idea.sector = cm.sector
            if cm.industry:
                idea.industry = cm.industry


_OVERCROWDING_THRESHOLD_PCT: float = 30.0
_TOP_TICKERS_PER_SECTOR: int = 5


def _compute_concentration(ideas: list) -> DiscoveryConcentration:
    """Aggregate enriched-tier scores by sector. Unclassified tickers (CIK-
    only spinoffs, ADRs yfinance hasn't tagged) get bucketed separately so
    the percentages don't lie when sector data is missing.
    """
    by_sector: dict[str, dict] = {}
    unclassified_total = 0.0
    grand_total = 0.0

    for idea in ideas:
        if idea.score <= 0:
            continue
        grand_total += idea.score
        sector = idea.sector
        if not sector:
            unclassified_total += idea.score
            continue
        bucket = by_sector.setdefault(sector, {"total": 0.0, "tickers": []})
        bucket["total"] += idea.score
        bucket["tickers"].append((idea.score, idea.ticker))

    sectors: list[SectorBreakdown] = []
    overcrowding: list[str] = []
    if grand_total > 0:
        for sector, bucket in by_sector.items():
            pct = bucket["total"] / grand_total * 100.0
            bucket["tickers"].sort(reverse=True)
            top = [t for _, t in bucket["tickers"][:_TOP_TICKERS_PER_SECTOR]]
            sectors.append(SectorBreakdown(
                sector=sector,
                score_total=round(bucket["total"], 2),
                score_pct=round(pct, 1),
                ticker_count=len(bucket["tickers"]),
                top_tickers=top,
            ))
            if pct >= _OVERCROWDING_THRESHOLD_PCT:
                overcrowding.append(sector)

    sectors.sort(key=lambda s: -s.score_total)
    unclassified_pct = (unclassified_total / grand_total * 100.0) if grand_total > 0 else 0.0

    return DiscoveryConcentration(
        sectors=sectors,
        overcrowding_threshold_pct=_OVERCROWDING_THRESHOLD_PCT,
        overcrowding_sectors=overcrowding,
        unclassified_pct=round(unclassified_pct, 1),
    )


async def _compute_universe() -> _CachedUniverse:
    """Run all 22 sources, persist snapshots, fire high-confluence alerts.
    Returns the full ranked universe (un-enriched). The only path that ever
    calls aggregate_ideas() — every page request slices this result.
    """
    aggregated = await aggregate_ideas()
    all_ideas = aggregated.ideas
    regime = aggregated.regime

    # Concentration computed once over a stable sample, NOT per-page —
    # otherwise sector mix would mutate as the user pages deeper.
    concentration = _compute_concentration(all_ideas[:_CONCENTRATION_SAMPLE_SIZE])

    _persist_snapshots(all_ideas)
    if _has_high_confluence(all_ideas):
        _trigger_high_confluence_alert_in_background()

    return _CachedUniverse(
        ideas=all_ideas,
        concentration=concentration,
        macro_regime=MacroRegimeSnapshot(
            mode=regime.mode,
            score_multiplier=regime.score_multiplier,
            reasons=regime.reasons,
            metrics=regime.metrics,
            as_of=regime.as_of,
        ),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


async def _get_or_compute_universe() -> _UniverseLookup:
    """Single-entry cache wrapper. Returns the cached universe + whether
    the response was served from cache. Single-flight de-dup so concurrent
    page requests share one cold compute.
    """
    entry = _cache.get(_CACHE_KEY)
    if entry is not None:
        universe, ts = entry
        if time.monotonic() - ts <= _CACHE_TTL_SECONDS:
            return _UniverseLookup(universe=universe, cached=True)
        _cache.pop(_CACHE_KEY, None)

    inflight = _inflight_refreshes.get(_CACHE_KEY)
    if inflight is not None and not inflight.done():
        result = await asyncio.shield(inflight)
        return _UniverseLookup(universe=result, cached=False)

    async def _run() -> _CachedUniverse:
        try:
            universe = await _compute_universe()
            _cache[_CACHE_KEY] = (universe, time.monotonic())
            return universe
        finally:
            _inflight_refreshes.pop(_CACHE_KEY, None)

    task = asyncio.create_task(_run())
    _inflight_refreshes[_CACHE_KEY] = task
    universe = await task
    return _UniverseLookup(universe=universe, cached=False)


async def get_ideas_page(page: int = 1, page_size: int = 100) -> DiscoveryResponse:
    """Return ONE page from the cached ranked universe.

    Cold compute runs once per hour (~30-40s). Subsequent pages slice the
    cached list and enrich just their slice (~3-5s, sub-50ms once the
    fundamentals 24h cache is warm).
    """
    page = max(1, page)
    page_size = max(1, min(page_size, 200))

    lookup = await _get_or_compute_universe()
    universe = lookup.universe
    total = len(universe.ideas)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start = (page - 1) * page_size
    end = start + page_size
    slice_ideas = universe.ideas[start:end]

    await _enrich_top_with_alpha(slice_ideas, max_enrich=page_size, days=30)

    return DiscoveryResponse(
        ideas=slice_ideas,
        total=total,
        cached=lookup.cached,
        generated_at=universe.generated_at,
        concentration=universe.concentration,
        macro_regime=universe.macro_regime,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        has_more=end < total,
    )


async def get_ideas(limit: int = 50) -> DiscoveryResponse:
    """Back-compat wrapper: returns page 1 with ``limit`` items. New code
    should prefer get_ideas_page(page, page_size) for explicit pagination.
    """
    return await get_ideas_page(page=1, page_size=limit)


def _to_snapshot_item(row: DiscoverySnapshot) -> DiscoverySnapshotItem:
    snapshot_at_iso = (
        row.snapshot_at.isoformat() if isinstance(row.snapshot_at, datetime) else str(row.snapshot_at or "")
    )
    return DiscoverySnapshotItem(
        ticker=row.ticker,
        cik=row.cik,
        company=row.company,
        score=row.score,
        distinct_sources=row.distinct_sources,
        snapshot_at=snapshot_at_iso,
    )


def get_history(ticker: str, days: int = 30, limit: int = 200) -> DiscoveryHistoryResponse:
    """Return snapshot time series for a ticker over the last N days."""
    sym = ticker.strip().upper()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    db: Session = SessionLocal()
    try:
        rows = (
            db.query(DiscoverySnapshot)
            .filter(
                DiscoverySnapshot.ticker == sym,
                DiscoverySnapshot.snapshot_at >= cutoff,
            )
            .order_by(DiscoverySnapshot.snapshot_at.asc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()

    items = [_to_snapshot_item(r) for r in rows]
    return DiscoveryHistoryResponse(
        ticker=sym,
        snapshots=items,
        total=len(items),
    )


def get_movers(days: int = 7, limit: int = 20, min_abs_delta: float = 20.0) -> DiscoveryMoversResponse:
    """Return tickers whose Discovery score moved by >= ``min_abs_delta`` over the window.

    For each ticker with snapshots in the lookback window, compares latest snapshot
    score to the oldest one in-window and reports the absolute delta. Sorted
    largest absolute movement first.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    db: Session = SessionLocal()
    try:
        rows = (
            db.query(DiscoverySnapshot)
            .filter(DiscoverySnapshot.snapshot_at >= cutoff)
            .order_by(DiscoverySnapshot.snapshot_at.asc())
            .all()
        )
    finally:
        db.close()

    by_ticker: dict[str, list[DiscoverySnapshot]] = {}
    for r in rows:
        by_ticker.setdefault(r.ticker, []).append(r)

    movers: list[DiscoveryMover] = []
    for ticker, snapshots in by_ticker.items():
        if len(snapshots) < 2:
            continue
        before = snapshots[0]
        now = snapshots[-1]
        delta = now.score - before.score
        if abs(delta) < min_abs_delta:
            continue
        movers.append(DiscoveryMover(
            ticker=ticker,
            cik=now.cik,
            company=now.company,
            score_now=now.score,
            score_before=before.score,
            delta=delta,
            snapshot_at_now=now.snapshot_at.isoformat() if isinstance(now.snapshot_at, datetime) else str(now.snapshot_at or ""),
            snapshot_at_before=before.snapshot_at.isoformat() if isinstance(before.snapshot_at, datetime) else str(before.snapshot_at or ""),
        ))

    movers.sort(key=lambda m: -abs(m.delta))
    movers = movers[:limit]
    return DiscoveryMoversResponse(movers=movers, days=days, total=len(movers))
