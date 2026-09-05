"""Tests for the Piotroski F-Score Discovery source.

The nine-test arithmetic is already covered in test_piotroski_dupont.py, so these
cover only what the source itself decides: the 7-and-above gate, and that thin or
failing data yields no idea rather than a fabricated one.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.backend.services.discovery_service._sources import piotroski_score as source


@pytest.fixture(autouse=True)
def _fixed_universe():
    """One ticker, so each test exercises scoring rather than the DB query."""
    with patch.object(source, "_gather_universe", return_value=["AAA"]):
        yield


@pytest.mark.parametrize(
    ("f_score", "emits"),
    [
        (9, True),
        (8, True),
        (7, True),
        (6, False),
        (3, False),
        (0, False),
    ],
    ids=["nine", "eight", "seven", "six_just_below", "distressed", "zero"],
)
async def test_only_a_strong_score_emits(f_score: int, emits: bool):
    with patch.object(source, "_score_for", AsyncMock(return_value=f_score)):
        result = await source.fetch()

    assert bool(result) is emits
    if emits:
        ticker, signal = result[0]
        assert ticker == "AAA"
        assert signal.source == "piotroski_score"
        assert signal.score == 20.0
        assert signal.detail["f_score"] == f_score


async def test_thin_statements_emit_nothing():
    # piotroski_score returns None when a fiscal year is missing; that must not
    # read as either strength or distress.
    with patch.object(source, "_score_for", AsyncMock(return_value=None)):
        assert await source.fetch() == []


async def test_a_per_ticker_failure_does_not_sink_the_source():
    with patch.object(source, "_score_for", AsyncMock(side_effect=RuntimeError("yfinance down"))):
        assert await source.fetch() == []


async def test_an_empty_universe_emits_nothing():
    with patch.object(source, "_gather_universe", return_value=[]):
        assert await source.fetch() == []


async def test_a_universe_query_failure_degrades_to_no_ideas():
    # The engine gathers sources with return_exceptions=True, so raising would
    # only register a dead source.
    with patch.object(source, "_gather_universe", side_effect=RuntimeError("db down")):
        assert await source.fetch() == []
