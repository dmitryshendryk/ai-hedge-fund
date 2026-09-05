"""Tests for the entry-quality exhaustion penalty applied during aggregation.

The penalty is the one place where Discovery scoring reacts to price. It must
subtract only for a genuinely extended name, and must never invent a score for
a ticker whose snapshot is unavailable.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.backend.models.discovery_schemas import DiscoveryIdea, IdeaSignal
from app.backend.services.discovery_service import _engine


class _Snapshot:
    def __init__(self, pct_above_sma: float):
        self.pct_above_sma = pct_above_sma
        self.rsi14 = None


def _idea(ticker: str, score: float) -> DiscoveryIdea:
    return DiscoveryIdea(
        ticker=ticker,
        score=score,
        signals=[IdeaSignal(source="relative_strength", score=score, label="x")],
    )


def _snapshots(mapping: dict[str, float]) -> AsyncMock:
    async def _get(ticker: str, **_kwargs):
        value = mapping.get(ticker.upper())
        return _Snapshot(value) if value is not None else None
    return AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_extended_ticker_loses_penalty_points():
    ideas = [_idea("ASML", 100.0)]

    with patch.object(_engine, "get_technical_snapshot", _snapshots({"ASML": 40.0})):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == 70.0
    assert ideas[0].pct_above_sma == 40.0
    assert ideas[0].exhaustion_penalty == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pct_above_sma", "expected_score"),
    [
        (29.9, 100.0),
        (30.0, 100.0),
        (30.1, 70.0),
        (0.0, 100.0),
        (-15.0, 100.0),
    ],
    ids=["below", "at_threshold", "above", "at_sma", "below_sma"],
)
async def test_penalty_applies_only_above_the_threshold(pct_above_sma: float, expected_score: float):
    # Threshold is exclusive: the requirement is "> 30", not ">= 30".
    ideas = [_idea("MU", 100.0)]

    with patch.object(_engine, "get_technical_snapshot", _snapshots({"MU": pct_above_sma})):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == expected_score


@pytest.mark.asyncio
async def test_score_never_goes_negative():
    ideas = [_idea("MU", 12.0)]

    with patch.object(_engine, "get_technical_snapshot", _snapshots({"MU": 55.0})):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == 0.0


@pytest.mark.asyncio
async def test_missing_snapshot_leaves_score_untouched():
    # An unpriceable ticker must not be penalized on absent evidence.
    ideas = [_idea("ZZZZ", 88.0)]

    with patch.object(_engine, "get_technical_snapshot", _snapshots({})):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == 88.0
    assert ideas[0].pct_above_sma is None
    assert ideas[0].exhaustion_penalty == 0.0


@pytest.mark.asyncio
async def test_snapshot_failure_leaves_score_untouched():
    ideas = [_idea("MU", 88.0)]

    with patch.object(_engine, "get_technical_snapshot", AsyncMock(side_effect=RuntimeError("yfinance down"))):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == 88.0


@pytest.mark.asyncio
async def test_cik_only_ideas_are_skipped():
    # A spin-off entity has no ticker, so there is no price series to fetch.
    ideas = [DiscoveryIdea(ticker="0001234567", score=50.0, signals=[], is_ticker=False)]
    snapshots = _snapshots({"0001234567": 99.0})

    with patch.object(_engine, "get_technical_snapshot", snapshots):
        await _engine.apply_exhaustion_penalty(ideas)

    assert ideas[0].score == 50.0
    snapshots.assert_not_awaited()


@pytest.mark.asyncio
async def test_reranks_after_penalty():
    # An extended leader must fall below a non-extended peer.
    ideas = [_idea("ASML", 100.0), _idea("LLY", 80.0)]

    with patch.object(_engine, "get_technical_snapshot", _snapshots({"ASML": 45.0, "LLY": 5.0})):
        await _engine.apply_exhaustion_penalty(ideas)

    assert [i.ticker for i in ideas] == ["LLY", "ASML"]


@pytest.mark.asyncio
async def test_only_the_top_slice_is_fetched():
    # One yfinance call per name over the whole universe is not affordable.
    ideas = [_idea(f"T{n}", float(300 - n)) for n in range(300)]
    snapshots = _snapshots({})

    with patch.object(_engine, "get_technical_snapshot", snapshots):
        await _engine.apply_exhaustion_penalty(ideas, max_check=50)

    assert snapshots.await_count == 50
