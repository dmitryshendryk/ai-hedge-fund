"""Tests for the industry field on CompanyMetrics."""

import pytest

from app.backend.services.fundamentals_service._metrics import _build_metrics_from_info


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"industry": "Semiconductor Equipment & Materials"}, "Semiconductor Equipment & Materials"),
        ({"industry": "Semiconductors"}, "Semiconductors"),
        ({}, None),
        # yfinance returns "" or None rather than omitting the key for some
        # tickers. Both must normalize to None so the concentration guard
        # buckets them as Unclassified instead of creating an empty-named bucket.
        ({"industry": ""}, None),
        ({"industry": None}, None),
    ],
    ids=["semicap", "semis", "key_absent", "empty_string", "explicit_none"],
)
def test_industry_extracted_from_info(info: dict, expected: str | None):
    assert _build_metrics_from_info("TEST", info).industry == expected
