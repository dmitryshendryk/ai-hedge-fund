"""Tests for concentration wiring on the positions list response.

The metrics fetch is best-effort: the position list is the page's primary
content, so a fundamentals outage must degrade concentration to None rather
than failing the request.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.backend.database.models import Position
from app.backend.services import fundamentals_service, pricing_service
from app.backend.services.position_service import list_positions_enriched


class _Metrics:
    def __init__(self, sector: str | None = None, industry: str | None = None):
        self.sector = sector
        self.industry = industry


class _Alpha:
    def __init__(self, price: float):
        self.end_price = price
        self.period_return_pct = 0.0
        self.alpha_pct = 0.0
        self.end_date = "2026-07-29"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def order_by(self, *_args):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Stands in for a Session; list_positions_enriched only queries and reads."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args):
        return _FakeQuery(self._rows)


def _row(ticker: str, shares: float, cost_basis: float) -> Position:
    return Position(id=1, ticker=ticker, shares=shares, cost_basis=cost_basis)


@pytest.fixture
def semi_book() -> _FakeSession:
    return _FakeSession([_row("MU", 10.0, 100.0), _row("LLY", 10.0, 100.0)])


@pytest.fixture
def priced() -> AsyncMock:
    return AsyncMock(return_value={"MU": _Alpha(100.0), "LLY": _Alpha(100.0)})


@pytest.mark.asyncio
async def test_concentration_present_when_metrics_available(semi_book, priced):
    metrics = AsyncMock(return_value={
        "MU": _Metrics("Technology", "Semiconductors"),
        "LLY": _Metrics("Healthcare", "Drug Manufacturers - General"),
    })
    with patch.object(pricing_service, "compute_alpha_batch", priced), \
         patch.object(fundamentals_service, "get_company_metrics_batch", metrics):
        result = await list_positions_enriched(semi_book)

    assert result.concentration is not None
    assert result.concentration.total_value == 2000.0


@pytest.mark.asyncio
async def test_concentration_is_none_when_metrics_raise(semi_book, priced):
    failing = AsyncMock(side_effect=RuntimeError("yfinance unavailable"))
    with patch.object(pricing_service, "compute_alpha_batch", priced), \
         patch.object(fundamentals_service, "get_company_metrics_batch", failing):
        result = await list_positions_enriched(semi_book)

    assert result.concentration is None
    assert result.total == 2  # the list itself still renders


@pytest.mark.asyncio
async def test_unresolved_metrics_yield_fully_unclassified_book(semi_book, priced):
    with patch.object(pricing_service, "compute_alpha_batch", priced), \
         patch.object(fundamentals_service, "get_company_metrics_batch", AsyncMock(return_value={})):
        result = await list_positions_enriched(semi_book)

    assert result.concentration.unclassified_pct == 100.0


@pytest.mark.asyncio
async def test_empty_book_skips_the_metrics_fetch():
    metrics = AsyncMock(return_value={})
    with patch.object(fundamentals_service, "get_company_metrics_batch", metrics):
        result = await list_positions_enriched(_FakeSession([]))

    assert result.concentration is None
    metrics.assert_not_awaited()
