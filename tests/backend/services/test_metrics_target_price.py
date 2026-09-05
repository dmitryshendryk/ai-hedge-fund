"""Tests for target_mean_price field on CompanyMetrics."""

from app.backend.services.fundamentals_service._metrics import _build_metrics_from_info


def test_target_mean_price_parsed():
    """target_mean_price is parsed from info["targetMeanPrice"]."""
    info = {"longName": "Test Co", "marketCap": 1_000_000_000, "targetMeanPrice": 250.5}
    m = _build_metrics_from_info("TEST", info)
    assert m.target_mean_price == 250.5


def test_target_mean_price_missing_is_none():
    """target_mean_price is None when missing from info dict."""
    info = {"longName": "Test Co", "marketCap": 1_000_000_000}
    m = _build_metrics_from_info("TEST", info)
    assert m.target_mean_price is None
