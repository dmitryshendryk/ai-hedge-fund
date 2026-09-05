"""Tests for the Kronos artifact reader and its two consumers.

The reader's job is refusing bad input: missing, malformed, or stale must read as
no signal, because a price forecast past its horizon is worse than none. The
consumers' job is refusing thin evidence — prob_up over a handful of sampled
paths is noise, and trading it is the failure these guard.

Needs no torch and no model weights; every case writes a synthetic artifact.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.services import kronos_service
from app.backend.services.devils_advocate_service._kronos_trend_exhaustion import (
    detect_kronos_trend_exhaustion,
)
from app.backend.services.devils_advocate_service._schemas import Severity
from app.backend.services.discovery_service._sources import kronos_predictive_surge


def _horizon(prob_up: float, expected: float) -> dict:
    return {
        "prob_up": prob_up,
        "expected_return_pct": expected,
        "p10_return_pct": expected - 5.0,
        "p90_return_pct": expected + 5.0,
    }


def _artifact(
    *,
    prob_up_5d: float = 0.80,
    expected_5d: float = 4.0,
    prob_up_7d: float = 0.10,
    expected_7d: float = -6.0,
    sample_count: int = 128,
    age_hours: float = 1.0,
    ticker: str = "AAA",
    schema_version: int = 1,
) -> dict:
    generated = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    return {
        "schema_version": schema_version,
        "generated_at": generated.isoformat(),
        "model": "NeoQuasar/Kronos-small",
        "params": {"lookback": 400, "pred_len": 7, "sample_count": sample_count, "seed": 42},
        "forecasts": {
            ticker: {
                "last_close": 100.0,
                "horizons": {"5": _horizon(prob_up_5d, expected_5d), "7": _horizon(prob_up_7d, expected_7d)},
            },
        },
    }


@pytest.fixture
def write_artifact(tmp_path, monkeypatch):
    """Point the reader at a temp artifact; clear its parse cache around each test."""
    path = tmp_path / "kronos_forecasts.json"
    monkeypatch.setenv("KRONOS_FORECAST_PATH", str(path))

    def _write(payload: dict | str) -> None:
        path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
        kronos_service.clear_cache()

    kronos_service.clear_cache()
    yield _write
    kronos_service.clear_cache()


@pytest.fixture
def no_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("KRONOS_FORECAST_PATH", str(tmp_path / "absent.json"))
    kronos_service.clear_cache()
    yield
    kronos_service.clear_cache()


class TestArtifactReader:
    def test_a_fresh_artifact_is_read(self, write_artifact):
        write_artifact(_artifact())

        forecast = kronos_service.get_forecast("AAA")

        assert forecast.sample_count == 128
        assert forecast.horizon(5).prob_up == 0.80
        assert forecast.horizon(5).expected_return_pct == 4.0

    def test_ticker_lookup_is_case_insensitive(self, write_artifact):
        # The worker may emit either case; a mismatch would silently drop every
        # forecast rather than fail loudly.
        write_artifact(_artifact(ticker="aaa"))

        assert kronos_service.get_forecast("AaA") is not None

    def test_an_unemitted_horizon_is_none(self, write_artifact):
        write_artifact(_artifact())

        assert kronos_service.get_forecast("AAA").horizon(30) is None

    @pytest.mark.parametrize(
        ("payload", "status"),
        [
            ("not json at all", kronos_service.KronosStatus.UNREADABLE),
            ("[1, 2, 3]", kronos_service.KronosStatus.UNREADABLE),
            (_artifact(schema_version=2), kronos_service.KronosStatus.SCHEMA_MISMATCH),
            (_artifact(age_hours=48.0), kronos_service.KronosStatus.STALE),
        ],
        ids=["malformed_json", "json_not_an_object", "future_schema", "stale"],
    )
    def test_unusable_content_yields_no_forecast(self, write_artifact, payload, status):
        # A future schema may reuse field names with different meanings, and a
        # stale forecast has already spent its horizon — both are refused.
        write_artifact(payload)

        assert kronos_service.get_status() is status
        assert kronos_service.get_all_forecasts() == {}

    def test_a_missing_file_yields_no_forecast(self, no_artifact):
        assert kronos_service.get_status() is kronos_service.KronosStatus.MISSING
        assert kronos_service.get_all_forecasts() == {}

    def test_an_entry_without_horizons_is_dropped(self, write_artifact):
        payload = _artifact()
        payload["forecasts"]["AAA"]["horizons"] = {}
        write_artifact(payload)

        assert kronos_service.get_status() is kronos_service.KronosStatus.EMPTY

    def test_a_rewritten_artifact_is_picked_up(self, write_artifact):
        # Invalidation keys on mtime, so a fresh worker run must not wait out
        # the parse cache's TTL.
        write_artifact(_artifact(prob_up_5d=0.80))
        assert kronos_service.get_forecast("AAA").horizon(5).prob_up == 0.80

        write_artifact(_artifact(prob_up_5d=0.20))

        assert kronos_service.get_forecast("AAA").horizon(5).prob_up == 0.20


class TestPredictiveSurgeSource:
    async def test_a_confident_upward_forecast_emits_a_signal(self, write_artifact):
        write_artifact(_artifact(prob_up_5d=0.80, expected_5d=4.0))

        result = await kronos_predictive_surge.fetch()

        assert len(result) == 1
        ticker, signal = result[0]
        assert ticker == "AAA"
        assert signal.source == "kronos_predictive_surge"
        assert signal.score == 25.0

    @pytest.mark.parametrize(
        ("prob_up", "expected", "sample_count"),
        [
            (0.69, 4.0, 128),
            (0.50, 4.0, 128),
            (0.85, -3.0, 128),
            (0.75, 4.0, 4),
        ],
        ids=["just_below_threshold", "coin_flip", "negative_skew", "too_few_paths"],
    )
    async def test_weak_evidence_emits_nothing(
        self, write_artifact, prob_up: float, expected: float, sample_count: int,
    ):
        # negative_skew: paths agree on direction but the mean is negative —
        # many small gains against a few large losses is a setup to avoid.
        # too_few_paths: 3 of 4 higher reads as 75% but is indistinguishable
        # from noise at that sample size.
        write_artifact(_artifact(prob_up_5d=prob_up, expected_5d=expected, sample_count=sample_count))

        assert await kronos_predictive_surge.fetch() == []

    async def test_a_missing_artifact_emits_nothing(self, no_artifact):
        assert await kronos_predictive_surge.fetch() == []


class TestKronosTrendExhaustionDetector:
    @pytest.mark.parametrize(
        ("prob_up_7d", "expected_7d"),
        [
            (0.10, -8.0),
            (0.30, -4.0),
        ],
        ids=["confident_decline", "moderate_decline"],
    )
    async def test_a_forecast_decline_is_reported_at_info_only(
        self, write_artifact, prob_up_7d: float, expected_7d: float,
    ):
        # Scored below the bands get_red_flags uses for WARNING and CRITICAL, so
        # an uncalibrated forecast can never escalate a report on its own.
        write_artifact(_artifact(prob_up_7d=prob_up_7d, expected_7d=expected_7d))

        findings = await detect_kronos_trend_exhaustion("AAA")

        assert len(findings) == 1
        assert findings[0].severity is Severity.INFO
        assert findings[0].score < 30.0
        assert findings[0].detail["prob_down"] == pytest.approx(1.0 - prob_up_7d)
        assert findings[0].detail["calibrated"] is False

    @pytest.mark.parametrize(
        ("prob_up_7d", "expected_7d", "sample_count"),
        [
            (0.85, 5.0, 128),
            (0.05, -0.4, 128),
            (0.00, -8.0, 4),
        ],
        ids=["confident_advance", "trivial_drop", "too_few_paths"],
    )
    async def test_no_flag_without_a_material_forecast_decline(
        self, write_artifact, prob_up_7d: float, expected_7d: float, sample_count: int,
    ):
        # trivial_drop: direction is agreed but the move is under a percent.
        write_artifact(_artifact(prob_up_7d=prob_up_7d, expected_7d=expected_7d, sample_count=sample_count))

        assert await detect_kronos_trend_exhaustion("AAA") == []

    async def test_an_unforecast_ticker_raises_nothing(self, write_artifact):
        write_artifact(_artifact(ticker="AAA"))

        assert await detect_kronos_trend_exhaustion("ZZZ") == []

    async def test_the_overlay_and_the_source_can_disagree(self, write_artifact):
        # A 5-day advance that fades by day 7 is information, not a conflict:
        # both readings come from one artifact and neither suppresses the other,
        # which is what keeps the overlay read-only.
        write_artifact(_artifact(prob_up_5d=0.80, expected_5d=4.0, prob_up_7d=0.10, expected_7d=-7.0))

        signals = await kronos_predictive_surge.fetch()
        findings = await detect_kronos_trend_exhaustion("AAA")

        assert len(signals) == 1
        assert len(findings) == 1


class TestKronosStaysOutOfLiveScoring:
    """Standing invariant, not a deletion guard.

    prob_up is uncalibrated — measured output clusters at 0.03/0.97 and the
    forecast sign flips with the lookback window. These assertions are meant to
    fail the moment someone wires Kronos into scoring, so tools/kronos_backtest.py
    has to justify it first.
    """

    def test_no_kronos_source_contributes_to_the_discovery_score(self):
        from app.backend.services.discovery_service._sources import SOURCES

        assert [name for name, _ in SOURCES if "kronos" in name] == []

    def test_no_kronos_detector_runs_in_the_red_flag_report(self):
        import inspect

        from app.backend.services import devils_advocate_service

        assert "kronos" not in inspect.getsource(devils_advocate_service.get_red_flags)
