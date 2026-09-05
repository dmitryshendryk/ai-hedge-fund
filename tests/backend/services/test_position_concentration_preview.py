"""Tests for the concentration preview endpoint.

Preview answers "what does buying this do to me?" before the position exists,
so its value is the before/after delta on the buckets the candidate lands in.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.backend.database.models import Position
from app.backend.services import fundamentals_service, pricing_service
from app.backend.services.position_service import preview_concentration


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
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_args):
        return _FakeQuery(self._rows)


_METRICS = {
    "MU": _Metrics("Technology", "Semiconductors"),
    "AMD": _Metrics("Technology", "Semiconductors"),
    "LLY": _Metrics("Healthcare", "Drug Manufacturers - General"),
    "META": _Metrics("Communication Services", "Internet Content & Information"),
}


def _book(*tickers: str) -> _FakeSession:
    return _FakeSession([Position(id=1, ticker=t, shares=10.0, cost_basis=100.0) for t in tickers])


def _priced(*tickers: str) -> AsyncMock:
    return AsyncMock(return_value={t: _Alpha(100.0) for t in tickers})


async def _preview(session, priced, ticker: str, amount: float):
    with patch.object(pricing_service, "compute_alpha_batch", priced), \
         patch.object(fundamentals_service, "get_company_metrics_batch", AsyncMock(return_value=_METRICS)):
        return await preview_concentration(session, ticker, amount)


@pytest.mark.asyncio
async def test_adding_a_semi_to_a_semi_heavy_book_raises_the_industry_weight():
    # $2000 book, half semis; adding $2000 of MU takes semis from 50% to 75%.
    result = await _preview(_book("AMD", "LLY"), _priced("AMD", "LLY"), "MU", 2000.0)

    assert result.industry == "Semiconductors"
    assert result.industry_weight_before_pct == 50.0
    assert result.industry_weight_after_pct == 75.0
    assert result.resulting_tier == "critical"


@pytest.mark.asyncio
async def test_adding_an_uncorrelated_name_leaves_the_new_sector_at_its_own_weight():
    result = await _preview(_book("AMD", "LLY"), _priced("AMD", "LLY"), "META", 2000.0)

    assert result.sector == "Communication Services"
    assert result.sector_weight_before_pct == 0.0
    assert result.sector_weight_after_pct == 50.0


@pytest.mark.asyncio
async def test_projected_book_includes_the_candidate():
    result = await _preview(_book("AMD"), _priced("AMD"), "MU", 1000.0)

    assert result.projected.total_value == 2000.0
    assert "MU" in [t for b in result.projected.positions for t in b.tickers]


@pytest.mark.asyncio
async def test_first_position_in_an_empty_book_is_the_whole_book():
    result = await _preview(_book(), _priced(), "MU", 5000.0)

    assert result.sector_weight_before_pct == 0.0
    assert result.sector_weight_after_pct == 100.0
    assert result.resulting_tier == "critical"


@pytest.mark.asyncio
async def test_unknown_ticker_is_unclassified_rather_than_an_error():
    result = await _preview(_book("AMD"), _priced("AMD"), "ZZZZ", 1000.0)

    assert result.sector is None
    assert result.industry is None
    # Unclassified is never tiered, so an unknown name cannot raise an alarm.
    assert result.resulting_tier == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", [0.0, -100.0], ids=["zero", "negative"])
async def test_non_positive_amount_is_rejected(amount: float):
    with pytest.raises(ValueError, match="Amount"):
        await preview_concentration(_book("AMD"), "MU", amount)


@pytest.mark.asyncio
async def test_empty_ticker_is_rejected():
    with pytest.raises(ValueError, match="Ticker"):
        await preview_concentration(_book("AMD"), "   ", 1000.0)
