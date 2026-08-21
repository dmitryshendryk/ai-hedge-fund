"""Tests for the automatic stop-loss stored when a position is added.

The stop exists so the exit level is fixed before the position moves, not
decided under pressure. It must be absent rather than guessed when ATR is
unavailable — a wrong stop is worse than none.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.backend.database.models import Base, Position
from app.backend.models.position_schemas import PositionAddRequest
from app.backend.services import pricing_service
from app.backend.services.position_service import add_position_with_stop, compute_stop_loss


class _Atr:
    def __init__(self, atr: float, latest_close: float = 100.0):
        self.ticker = "MU"
        self.atr = atr
        self.latest_close = latest_close
        self.atr_pct_of_price = atr / latest_close * 100.0


@pytest.fixture
def position_db() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Position.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


class TestComputeStopLoss:
    """Stop level arithmetic: entry - (multiple * ATR)."""

    def test_stop_sits_two_atr_below_entry(self):
        assert compute_stop_loss(cost_basis=575.0, atr=12.5) == 550.0

    def test_a_volatile_name_gets_a_wider_stop(self):
        quiet = compute_stop_loss(cost_basis=100.0, atr=1.0)
        volatile = compute_stop_loss(cost_basis=100.0, atr=10.0)

        assert volatile < quiet

    def test_stop_never_goes_below_zero(self):
        # A low-priced name with a large relative range would otherwise go negative.
        assert compute_stop_loss(cost_basis=3.0, atr=5.0) == 0.0

    @pytest.mark.parametrize("atr", [None, 0.0, -1.0], ids=["missing", "zero", "negative"])
    def test_no_stop_without_a_usable_atr(self, atr: float | None):
        assert compute_stop_loss(cost_basis=575.0, atr=atr) is None


@pytest.mark.asyncio
async def test_added_position_stores_its_stop(position_db: Session):
    with patch.object(pricing_service, "get_atr", AsyncMock(return_value=_Atr(12.5))):
        result = await add_position_with_stop(
            position_db, PositionAddRequest(ticker="MU", shares=2.0, cost_basis=575.0),
        )

    assert result.stop_loss_price == 550.0
    assert result.stop_atr == 12.5
    assert result.stop_multiple == 2.0


@pytest.mark.asyncio
async def test_position_still_saves_when_atr_is_unavailable(position_db: Session):
    with patch.object(pricing_service, "get_atr", AsyncMock(return_value=None)):
        result = await add_position_with_stop(
            position_db, PositionAddRequest(ticker="ZZZZ", shares=1.0, cost_basis=10.0),
        )

    assert result.ticker == "ZZZZ"
    assert result.stop_loss_price is None


@pytest.mark.asyncio
async def test_atr_failure_does_not_block_the_add(position_db: Session):
    with patch.object(pricing_service, "get_atr", AsyncMock(side_effect=RuntimeError("yfinance down"))):
        result = await add_position_with_stop(
            position_db, PositionAddRequest(ticker="MU", shares=1.0, cost_basis=575.0),
        )

    assert result.stop_loss_price is None


@pytest.mark.asyncio
async def test_re_adding_recomputes_the_stop_from_the_blended_cost(position_db: Session):
    # add_position averages into the existing lot, so the stop must follow the
    # new blended basis rather than keep the level from the first purchase.
    with patch.object(pricing_service, "get_atr", AsyncMock(return_value=_Atr(10.0))):
        await add_position_with_stop(position_db, PositionAddRequest(ticker="MU", shares=1.0, cost_basis=100.0))
        result = await add_position_with_stop(position_db, PositionAddRequest(ticker="MU", shares=1.0, cost_basis=200.0))

    assert result.cost_basis == 150.0
    assert result.stop_loss_price == 130.0
