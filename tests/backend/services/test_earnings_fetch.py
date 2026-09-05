"""Tests for EDGAR filing-list normalisation in the earnings fetcher.

edgartools returns None when a company has no filings of the requested form,
and a bare Filing rather than a list when only one matches. Iterating either
raises, which surfaced as a 500 on /insider/earnings/analysis.
"""

import pytest

from app.backend.services.earnings_service._fetch import _as_filing_list


class _Filing:
    """Stands in for an edgar Filing, which is not iterable."""


class _FilingsPage:
    """Stands in for an edgar filings collection: iterable but not a list."""

    def __init__(self, items):
        self._items = items

    def __iter__(self):
        return iter(self._items)


def test_none_becomes_an_empty_list():
    assert _as_filing_list(None) == []


def test_a_single_filing_is_wrapped():
    filing = _Filing()

    assert _as_filing_list(filing) == [filing]


def test_a_list_passes_through():
    filings = [_Filing(), _Filing()]

    assert _as_filing_list(filings) == filings


def test_a_non_list_iterable_is_materialised():
    filings = [_Filing(), _Filing()]

    assert _as_filing_list(_FilingsPage(filings)) == filings


def test_an_empty_collection_stays_empty():
    assert _as_filing_list(_FilingsPage([])) == []


@pytest.mark.parametrize("value", ["AAPL", 5], ids=["string", "int"])
def test_a_scalar_is_wrapped_rather_than_exploded(value):
    # A string is iterable but is not a collection of filings; iterating it
    # would yield characters and corrupt the caller's loop.
    assert _as_filing_list(value) == [value]
