"""Tests for the Discovery universe cache and the pricing snapshot cache.

Both exist to stop one Discovery refresh from repeating the same upstream call.
The tests therefore count compute invocations rather than inspecting cache
internals: a cache that stores entries but still refetches would pass any
structural assertion while costing exactly what it was built to save.
"""

import asyncio
from unittest.mock import patch

import pytest

from app.backend.services import discovery_service, pricing_service


@pytest.fixture(autouse=True)
def _clean_caches():
    discovery_service._cache.clear()
    discovery_service._inflight_refreshes.clear()
    pricing_service._snapshot_cache.clear()
    pricing_service._inflight_locks.clear()
    pricing_service._yfinance_cooldown_until = 0.0
    yield
    discovery_service._cache.clear()
    discovery_service._inflight_refreshes.clear()
    pricing_service._snapshot_cache.clear()
    pricing_service._inflight_locks.clear()


class _Universe:
    """Stands in for _CachedUniverse; only identity and .ideas are read here."""

    def __init__(self, tag: str):
        self.ideas = []
        self.tag = tag


class TestDiscoveryUniverseCache:
    def test_ttl_is_four_hours(self):
        # The window the flush button exists to cut short.
        assert discovery_service.get_cache_ttl_seconds() == 4 * 3600.0

    @pytest.mark.asyncio
    async def test_a_second_request_inside_the_window_does_not_recompute(self):
        calls = []

        async def _compute():
            calls.append(1)
            return _Universe("first")

        with patch.object(discovery_service, "_compute_universe", _compute):
            first = await discovery_service._get_or_compute_universe()
            second = await discovery_service._get_or_compute_universe()

        assert len(calls) == 1
        assert first.cached is False
        assert second.cached is True
        assert second.universe is first.universe

    @pytest.mark.asyncio
    async def test_an_expired_entry_recomputes(self):
        calls = []

        async def _compute():
            calls.append(1)
            return _Universe(f"run{len(calls)}")

        with patch.object(discovery_service, "_compute_universe", _compute):
            await discovery_service._get_or_compute_universe()
            # Age the entry past the TTL rather than waiting four hours.
            universe, stamped = discovery_service._cache[discovery_service._CACHE_KEY]
            discovery_service._cache[discovery_service._CACHE_KEY] = (
                universe,
                stamped - discovery_service.get_cache_ttl_seconds() - 1.0,
            )
            result = await discovery_service._get_or_compute_universe()

        assert len(calls) == 2
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_flush_forces_the_next_request_to_recompute(self):
        calls = []

        async def _compute():
            calls.append(1)
            return _Universe(f"run{len(calls)}")

        with patch.object(discovery_service, "_compute_universe", _compute):
            await discovery_service._get_or_compute_universe()
            report = discovery_service.flush_cache()
            after = await discovery_service._get_or_compute_universe()

        assert report["discovery_ideas"] == 1
        assert len(calls) == 2
        assert after.cached is False

    def test_flushing_an_empty_cache_reports_nothing_cleared(self):
        # The UI distinguishes "cleared" from "was already empty".
        assert discovery_service.flush_cache() == {
            "discovery_ideas": 0,
            "discovery_inflight": 0,
        }

    @pytest.mark.asyncio
    async def test_flush_cancels_an_in_flight_refresh(self):
        # A compute that started before the flush must not be awaited by the
        # next caller — it would hand back the ranking the user discarded.
        started = asyncio.Event()

        async def _slow():
            started.set()
            await asyncio.sleep(30)
            return _Universe("stale")

        with patch.object(discovery_service, "_compute_universe", _slow):
            task = asyncio.create_task(discovery_service._get_or_compute_universe())
            await started.wait()
            await asyncio.sleep(0)
            report = discovery_service.flush_cache()

        assert report["discovery_inflight"] == 1
        assert discovery_service._inflight_refreshes == {}
        with pytest.raises(asyncio.CancelledError):
            await task


class TestPricingSnapshotCache:
    @pytest.mark.asyncio
    async def test_a_repeated_key_fetches_once(self):
        calls = []

        def _compute():
            calls.append(1)
            return "snapshot"

        first = await pricing_service._cached_snapshot(("kind", "AAA"), _compute)
        second = await pricing_service._cached_snapshot(("kind", "AAA"), _compute)

        assert len(calls) == 1
        assert first == second == "snapshot"

    @pytest.mark.asyncio
    async def test_distinct_keys_each_fetch(self):
        calls = []

        def _compute():
            calls.append(1)
            return "snapshot"

        await pricing_service._cached_snapshot(("kind", "AAA"), _compute)
        await pricing_service._cached_snapshot(("kind", "BBB"), _compute)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_an_unpriceable_ticker_is_not_refetched(self):
        # None is a real answer, so it must cache — otherwise a delisted symbol
        # costs one request per idea that mentions it.
        calls = []

        def _compute():
            calls.append(1)
            return None

        assert await pricing_service._cached_snapshot(("kind", "DEAD"), _compute) is None
        assert await pricing_service._cached_snapshot(("kind", "DEAD"), _compute) is None
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_fetch(self):
        calls = []

        def _compute():
            calls.append(1)
            return "snapshot"

        results = await asyncio.gather(*(
            pricing_service._cached_snapshot(("kind", "AAA"), _compute) for _ in range(5)
        ))

        assert len(calls) == 1
        assert results == ["snapshot"] * 5

    @pytest.mark.asyncio
    async def test_a_rate_limit_is_not_cached(self):
        # Caching a throttled call would pin None for the whole TTL window.
        def _throttled():
            raise pricing_service._RateLimited("Too Many Requests")

        assert await pricing_service._cached_snapshot(("kind", "AAA"), _throttled) is None
        assert ("kind", "AAA") not in pricing_service._snapshot_cache

    @pytest.mark.asyncio
    async def test_an_expired_entry_refetches(self):
        calls = []

        def _compute():
            calls.append(1)
            return "snapshot"

        key = ("kind", "AAA")
        await pricing_service._cached_snapshot(key, _compute)
        value, stamped = pricing_service._snapshot_cache[key]
        pricing_service._snapshot_cache[key] = (
            value,
            stamped - pricing_service._SNAPSHOT_CACHE_TTL_SECONDS - 1.0,
        )
        await pricing_service._cached_snapshot(key, _compute)

        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_the_cache_is_bounded(self):
        for index in range(pricing_service._SNAPSHOT_CACHE_MAX_SIZE + 10):
            await pricing_service._cached_snapshot(("kind", str(index)), lambda: "snapshot")

        assert len(pricing_service._snapshot_cache) <= pricing_service._SNAPSHOT_CACHE_MAX_SIZE
