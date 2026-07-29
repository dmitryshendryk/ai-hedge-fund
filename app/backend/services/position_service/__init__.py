"""Position service: CRUD for holdings + live price / P&L enrichment.

Holdings power the stop_loss exit alert and the portfolio P&L page. Live
pricing reuses pricing_service.compute_alpha_batch — one batched yfinance
pass returns current price (latest close), return since entry, and SPY alpha
per ticker; unrealized P&L against cost basis is derived from that close.
See Also: pricing_service for cache/cooldown behavior.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy.orm import Session

from app.backend.database.models import Position
from app.backend.models.position_schemas import (
    ConcentrationBucket,
    PortfolioConcentration,
    PositionAddRequest,
    PositionListResponse,
    PositionResponse,
    PositionUpdateRequest,
)

logger = logging.getLogger(__name__)

# Concentration tiers. Calibrated so a book holding four correlated semis
# clears "critical" while a broadly diversified book clears nothing. Industry
# thresholds sit below sector ones because a single industry is the tighter,
# more dangerous bet.
_SECTOR_WARN_PCT: float = 35.0
_SECTOR_CRITICAL_PCT: float = 50.0
_INDUSTRY_WARN_PCT: float = 25.0
_INDUSTRY_CRITICAL_PCT: float = 40.0
_POSITION_WARN_PCT: float = 20.0
_POSITION_CRITICAL_PCT: float = 30.0

_UNCLASSIFIED = "Unclassified"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value or "")


def _parse_entry_date(raw: str | None) -> datetime:
    """Parse an optional ISO date/datetime, defaulting to now (UTC)."""
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _base_response(item: Position) -> PositionResponse:
    """Static (un-priced) view — the row always renders even if pricing fails."""
    return PositionResponse(
        id=item.id,
        ticker=item.ticker,
        shares=item.shares,
        cost_basis=item.cost_basis,
        entry_date=_iso(item.entry_date),
        notes=item.notes,
        cost_value=round(item.shares * item.cost_basis, 2),
    )


def add_position(db: Session, req: PositionAddRequest) -> PositionResponse:
    sym = req.ticker.strip().upper()
    if not sym:
        raise ValueError("Ticker cannot be empty")
    if req.shares <= 0:
        raise ValueError("Shares must be positive")
    if req.cost_basis <= 0:
        raise ValueError("Cost basis must be positive")

    entry_dt = _parse_entry_date(req.entry_date)
    existing = db.query(Position).filter(Position.ticker == sym).first()
    if existing:
        # Re-adding an owned ticker averages into the existing lot rather than
        # rejecting — matches how a broker reports a single blended position.
        total_shares = existing.shares + req.shares
        existing.cost_basis = (
            existing.shares * existing.cost_basis + req.shares * req.cost_basis
        ) / total_shares
        existing.shares = total_shares
        if req.notes is not None:
            existing.notes = req.notes
        db.commit()
        db.refresh(existing)
        return _base_response(existing)

    item = Position(
        ticker=sym,
        shares=req.shares,
        cost_basis=req.cost_basis,
        entry_date=entry_dt,
        notes=req.notes,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return _base_response(item)


def update_position(db: Session, ticker: str, req: PositionUpdateRequest) -> PositionResponse | None:
    sym = ticker.strip().upper()
    item = db.query(Position).filter(Position.ticker == sym).first()
    if item is None:
        return None
    if req.shares is not None:
        if req.shares <= 0:
            raise ValueError("Shares must be positive")
        item.shares = req.shares
    if req.cost_basis is not None:
        if req.cost_basis <= 0:
            raise ValueError("Cost basis must be positive")
        item.cost_basis = req.cost_basis
    if req.entry_date is not None:
        item.entry_date = _parse_entry_date(req.entry_date)
    if req.notes is not None:
        item.notes = req.notes
    db.commit()
    db.refresh(item)
    return _base_response(item)


def remove_position(db: Session, ticker: str) -> bool:
    sym = ticker.strip().upper()
    deleted = db.query(Position).filter(Position.ticker == sym).delete()
    db.commit()
    return deleted > 0


def _position_value(item: PositionResponse) -> float:
    """Market value when priced, else cost basis.

    Risk measurement differs from P&L here: list_positions_enriched excludes
    unpriced names from market-value totals, but a position that cannot be
    priced is still capital at risk, so it must stay in the denominator.
    """
    return item.market_value if item.market_value is not None else item.cost_value


def _tier(weight_pct: float, warn: float, critical: float) -> str:
    if weight_pct >= critical:
        return "critical"
    if weight_pct >= warn:
        return "warn"
    return "ok"


def _buckets(
    grouped: dict[str, tuple[float, list[str]]],
    total_value: float,
    warn: float,
    critical: float,
) -> list[ConcentrationBucket]:
    out = [
        ConcentrationBucket(
            name=name,
            value=round(value, 2),
            weight_pct=round(value / total_value * 100.0, 2),
            tickers=sorted(tickers),
            # An index-fund sleeve is diversification, not a factor bet, so the
            # catch-all bucket is never tiered however large it grows.
            tier="ok" if name == _UNCLASSIFIED else _tier(value / total_value * 100.0, warn, critical),
        )
        for name, (value, tickers) in grouped.items()
    ]
    out.sort(key=lambda b: -b.weight_pct)
    return out


def compute_concentration(items: list[PositionResponse], metrics_by_ticker: dict) -> PortfolioConcentration | None:
    """Sector / industry / single-name concentration over the held book.

    Pure: no DB or network access. Callers supply metrics they already batched.

    Args:
        items: Enriched positions; `market_value` may be None when unpriced.
        metrics_by_ticker: Upper-case ticker to CompanyMetrics (or None when
            yfinance could not resolve it), supplying sector and industry.

    Returns:
        None for an empty or zero-value book, where weights are undefined.
    """
    if not items:
        return None

    total_value = sum(_position_value(i) for i in items)
    if total_value <= 0:
        return None

    priced = sum(1 for i in items if i.market_value is not None)
    if priced == len(items):
        valued_on = "market"
    elif priced == 0:
        valued_on = "cost"
    else:
        valued_on = "mixed"

    by_sector: dict[str, tuple[float, list[str]]] = {}
    by_industry: dict[str, tuple[float, list[str]]] = {}
    by_position: dict[str, tuple[float, list[str]]] = {}
    unclassified_value = 0.0

    for item in items:
        ticker = item.ticker.upper()
        value = _position_value(item)
        metrics = metrics_by_ticker.get(ticker)
        sector = (getattr(metrics, "sector", None) or "").strip() or _UNCLASSIFIED
        industry = (getattr(metrics, "industry", None) or "").strip() or _UNCLASSIFIED
        if sector == _UNCLASSIFIED:
            unclassified_value += value

        for grouped, key in ((by_sector, sector), (by_industry, industry), (by_position, ticker)):
            prev_value, prev_tickers = grouped.get(key, (0.0, []))
            grouped[key] = (prev_value + value, [*prev_tickers, ticker])

    sectors = _buckets(by_sector, total_value, _SECTOR_WARN_PCT, _SECTOR_CRITICAL_PCT)
    industries = _buckets(by_industry, total_value, _INDUSTRY_WARN_PCT, _INDUSTRY_CRITICAL_PCT)
    positions = _buckets(by_position, total_value, _POSITION_WARN_PCT, _POSITION_CRITICAL_PCT)

    warnings = [
        f"{b.name} is {b.weight_pct:.0f}% of your book ({b.tier})"
        for tier in ("critical", "warn")
        for b in sectors + industries + positions
        if b.tier == tier
    ]

    return PortfolioConcentration(
        total_value=round(total_value, 2),
        valued_on=valued_on,
        sectors=sectors,
        industries=industries,
        positions=positions,
        warnings=warnings,
        unclassified_pct=round(unclassified_value / total_value * 100.0, 2),
        sector_warn_pct=_SECTOR_WARN_PCT,
        sector_critical_pct=_SECTOR_CRITICAL_PCT,
        industry_warn_pct=_INDUSTRY_WARN_PCT,
        industry_critical_pct=_INDUSTRY_CRITICAL_PCT,
    )


async def _safe_concentration(items: list[PositionResponse]) -> PortfolioConcentration | None:
    """Concentration for the book, or None if fundamentals are unavailable.

    Best-effort by design: the holdings list is the page's primary content and
    must survive a fundamentals outage.
    """
    if not items:
        return None
    try:
        from app.backend.services.fundamentals_service import get_company_metrics_batch

        metrics_by_ticker = await get_company_metrics_batch([i.ticker for i in items])
        return compute_concentration(items, metrics_by_ticker)
    except Exception as exc:
        logger.debug("positions: concentration unavailable: %s", exc)
        return None


def _entry_since(item: Position) -> date:
    if isinstance(item.entry_date, datetime):
        return item.entry_date.astimezone(timezone.utc).date() if item.entry_date.tzinfo else item.entry_date.date()
    return date.today()


async def list_positions_enriched(db: Session) -> PositionListResponse:
    """All positions with live price + P&L, plus portfolio-level roll-ups.

    Priced tickers contribute to the portfolio totals; unpriced ones are
    excluded from market-value sums (so a single delisted name doesn't zero
    out the whole portfolio) but still appear as rows.
    """
    from app.backend.services.pricing_service import compute_alpha_batch

    rows = db.query(Position).order_by(Position.ticker.asc()).all()
    items = [_base_response(r) for r in rows]
    total_cost_value = round(sum(i.cost_value for i in items), 2)

    if not rows:
        return PositionListResponse(items=items, total=0, total_cost_value=0.0)

    pairs: list[tuple[str, date]] = [(r.ticker, _entry_since(r)) for r in rows]
    metrics_by_ticker = await compute_alpha_batch(pairs)

    by_ticker = {i.ticker.upper(): i for i in items}
    cost_by_ticker = {r.ticker.upper(): r.shares * r.cost_basis for r in rows}
    shares_by_ticker = {r.ticker.upper(): r.shares for r in rows}

    total_market_value = 0.0
    priced_cost_value = 0.0
    any_priced = False
    for ticker_upper, metrics in metrics_by_ticker.items():
        target = by_ticker.get(ticker_upper)
        if target is None or metrics is None:
            continue
        any_priced = True
        price = metrics.end_price
        shares = shares_by_ticker[ticker_upper]
        market_value = price * shares
        cost_value = cost_by_ticker[ticker_upper]

        target.current_price = round(price, 2)
        target.market_value = round(market_value, 2)
        target.unrealized_pnl = round(market_value - cost_value, 2)
        target.unrealized_pnl_pct = round((price / target.cost_basis - 1.0) * 100.0, 2)
        target.return_since_entry_pct = round(metrics.period_return_pct, 2)
        target.alpha_pct_vs_spy = round(metrics.alpha_pct, 2)
        target.price_as_of = metrics.end_date

        total_market_value += market_value
        priced_cost_value += cost_value

    if not any_priced:
        return PositionListResponse(
            items=items,
            total=len(items),
            total_cost_value=total_cost_value,
            concentration=await _safe_concentration(items),
        )

    total_pnl = total_market_value - priced_cost_value
    total_pnl_pct = (total_market_value / priced_cost_value - 1.0) * 100.0 if priced_cost_value > 0 else None
    return PositionListResponse(
        items=items,
        total=len(items),
        total_cost_value=total_cost_value,
        total_market_value=round(total_market_value, 2),
        total_unrealized_pnl=round(total_pnl, 2),
        total_unrealized_pnl_pct=round(total_pnl_pct, 2) if total_pnl_pct is not None else None,
        concentration=await _safe_concentration(items),
    )
