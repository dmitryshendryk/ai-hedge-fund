"""Tests for compute_concentration: factor concentration over owned capital.

The concentrated-book and diversified-book cases encode the calibration from
the design doc's acceptance criteria — if the thresholds drift, those fail.
"""

import pytest

from app.backend.models.position_schemas import PositionResponse
from app.backend.services.position_service import compute_concentration


class _Metrics:
    """Stand-in for CompanyMetrics; compute_concentration reads only these."""

    def __init__(self, sector: str | None = None, industry: str | None = None):
        self.sector = sector
        self.industry = industry


def _pos(ticker: str, market_value: float | None, cost_value: float | None = None) -> PositionResponse:
    cost = cost_value if cost_value is not None else market_value
    return PositionResponse(
        id=1,
        ticker=ticker,
        shares=1.0,
        cost_basis=cost,
        entry_date="2026-06-29",
        cost_value=cost,
        market_value=market_value,
    )


def _bucket(buckets, name: str):
    return next(b for b in buckets if b.name == name)


_SEMI_METRICS = {
    "MU": _Metrics("Technology", "Semiconductors"),
    "AMD": _Metrics("Technology", "Semiconductors"),
    "ASML": _Metrics("Technology", "Semiconductor Equipment & Materials"),
    "LRCX": _Metrics("Technology", "Semiconductor Equipment & Materials"),
    "LLY": _Metrics("Healthcare", "Drug Manufacturers - General"),
}


def test_empty_book_has_no_concentration():
    assert compute_concentration([], {}) is None


def test_zero_value_book_has_no_concentration():
    # Guards the divide-by-zero path rather than emitting 0%-weight buckets.
    assert compute_concentration([_pos("ZZZZ", 0.0)], {}) is None


def test_concentrated_semi_book_is_critical_at_sector_and_industry():
    items = [_pos(t, 4000.0) for t in ("MU", "ASML", "AMD", "LRCX", "LLY")]

    result = compute_concentration(items, _SEMI_METRICS)

    tech = _bucket(result.sectors, "Technology")
    assert tech.weight_pct == 80.0
    assert tech.tier == "critical"
    semis = _bucket(result.industries, "Semiconductors")
    assert semis.weight_pct == 40.0
    assert semis.tier == "critical"
    assert result.valued_on == "market"
    assert any("Technology" in w for w in result.warnings)


def test_warnings_are_ordered_critical_first():
    items = [_pos(t, 4000.0) for t in ("MU", "ASML", "AMD", "LRCX", "LLY")]

    warnings = compute_concentration(items, _SEMI_METRICS).warnings

    assert "critical" in warnings[0]


def test_diversified_book_trips_no_critical_bucket():
    items = [_pos(t, 4000.0) for t in ("QQQ", "VTI", "LLY", "META", "NVDA")]
    metrics = {
        "QQQ": _Metrics(),
        "VTI": _Metrics(),
        "LLY": _Metrics("Healthcare", "Drug Manufacturers - General"),
        "META": _Metrics("Communication Services", "Internet Content & Information"),
        "NVDA": _Metrics("Technology", "Semiconductors"),
    }

    result = compute_concentration(items, metrics)

    assert all(b.tier != "critical" for b in result.sectors + result.industries)
    assert result.unclassified_pct == 40.0


def test_equal_weight_five_name_book_raises_no_warning_at_all():
    # Equal weight across five names is diversification. Warning on it would
    # train the reader to ignore warnings, so the guard must stay silent.
    items = [_pos(t, 4000.0) for t in ("QQQ", "VTI", "LLY", "META", "NVDA")]
    metrics = {
        "QQQ": _Metrics(),
        "VTI": _Metrics(),
        "LLY": _Metrics("Healthcare", "Drug Manufacturers - General"),
        "META": _Metrics("Communication Services", "Internet Content & Information"),
        "NVDA": _Metrics("Technology", "Semiconductors"),
    }

    assert compute_concentration(items, metrics).warnings == []


def test_unclassified_bucket_is_never_tiered():
    # A 100% index-fund book is not a concentrated factor bet.
    items = [_pos("VTI", 5000.0), _pos("QQQ", 5000.0)]

    result = compute_concentration(items, {"VTI": _Metrics(), "QQQ": _Metrics()})

    assert _bucket(result.sectors, "Unclassified").tier == "ok"
    assert result.unclassified_pct == 100.0


def test_unpriced_position_uses_cost_basis_and_stays_in_denominator():
    # Dropping it would understate Technology as 0% of a 4000 book.
    items = [_pos("MU", None, cost_value=6000.0), _pos("LLY", 4000.0)]

    result = compute_concentration(items, _SEMI_METRICS)

    assert result.total_value == 10000.0
    assert result.valued_on == "mixed"
    assert _bucket(result.sectors, "Technology").weight_pct == 60.0


def test_fully_unpriced_book_reports_cost_valuation():
    result = compute_concentration([_pos("MU", None, cost_value=5000.0)], _SEMI_METRICS)

    assert result.valued_on == "cost"
    assert result.total_value == 5000.0


def test_single_position_book_is_critical_on_that_position():
    result = compute_concentration([_pos("MU", 5000.0)], _SEMI_METRICS)

    assert _bucket(result.positions, "MU").tier == "critical"


def test_ticker_absent_from_metrics_is_unclassified():
    result = compute_concentration([_pos("ZZZZ", 1000.0)], {})

    assert result.unclassified_pct == 100.0
    assert _bucket(result.sectors, "Unclassified").weight_pct == 100.0


def test_metrics_value_of_none_is_unclassified():
    # get_company_metrics_batch maps unresolvable tickers to None, not absence.
    result = compute_concentration([_pos("ZZZZ", 1000.0)], {"ZZZZ": None})

    assert result.unclassified_pct == 100.0


def test_buckets_are_sorted_by_weight_descending():
    items = [_pos("LLY", 1000.0), _pos("MU", 6000.0), _pos("AMD", 3000.0)]

    sectors = compute_concentration(items, _SEMI_METRICS).sectors

    assert [b.weight_pct for b in sectors] == sorted((b.weight_pct for b in sectors), reverse=True)


def test_same_industry_bucket_aggregates_its_tickers():
    items = [_pos("MU", 2000.0), _pos("AMD", 2000.0), _pos("LLY", 6000.0)]

    semis = _bucket(compute_concentration(items, _SEMI_METRICS).industries, "Semiconductors")

    assert sorted(semis.tickers) == ["AMD", "MU"]
    assert semis.value == 4000.0


@pytest.mark.parametrize(
    ("tech_value", "expected_tier"),
    [
        (3400.0, "ok"),
        (3500.0, "warn"),
        (4900.0, "warn"),
        (5000.0, "critical"),
    ],
    ids=["below_warn", "at_warn", "below_critical", "at_critical"],
)
def test_sector_tier_boundaries_are_inclusive(tech_value: float, expected_tier: str):
    items = [_pos("MU", tech_value), _pos("LLY", 10000.0 - tech_value)]

    result = compute_concentration(items, _SEMI_METRICS)

    assert _bucket(result.sectors, "Technology").tier == expected_tier


@pytest.mark.parametrize(
    ("semi_value", "expected_tier"),
    [
        (2400.0, "ok"),
        (2500.0, "warn"),
        (3900.0, "warn"),
        (4000.0, "critical"),
    ],
    ids=["below_warn", "at_warn", "below_critical", "at_critical"],
)
def test_industry_tier_boundaries_are_inclusive(semi_value: float, expected_tier: str):
    items = [_pos("MU", semi_value), _pos("LLY", 10000.0 - semi_value)]

    result = compute_concentration(items, _SEMI_METRICS)

    assert _bucket(result.industries, "Semiconductors").tier == expected_tier
