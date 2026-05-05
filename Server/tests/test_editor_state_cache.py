"""Tests for the TTL + single-flight editor-state cache."""
from __future__ import annotations

import asyncio

import pytest

from services.state import editor_state_cache as cache


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.invalidate()
    yield
    cache.invalidate()


@pytest.mark.asyncio
async def test_caches_successful_results_within_ttl():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"success": True, "data": {"value": 42}}

    key = ("inst-a", None)

    a = await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)
    b = await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)

    assert a is b
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_does_not_cache_unsuccessful_results():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"success": False, "error": "transient"}

    key = ("inst-a", None)

    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)
    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_does_not_cache_exceptions():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        raise RuntimeError("boom")

    key = ("inst-a", None)

    with pytest.raises(RuntimeError):
        await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)
    with pytest.raises(RuntimeError):
        await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_single_flight_collapses_concurrent_callers():
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch():
        calls["n"] += 1
        started.set()
        await release.wait()
        return {"success": True, "data": {"value": "shared"}}

    key = ("inst-a", None)

    fan_out = [
        asyncio.create_task(
            cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)
        )
        for _ in range(5)
    ]
    await started.wait()
    release.set()
    results = await asyncio.gather(*fan_out)

    assert calls["n"] == 1
    # All callers receive the same payload object
    first = results[0]
    for r in results[1:]:
        assert r is first


@pytest.mark.asyncio
async def test_keys_are_isolated():
    calls = {"a": 0, "b": 0}

    async def make_fetch(name: str):
        async def fetch():
            calls[name] += 1
            return {"success": True, "data": {"name": name}}
        return fetch

    fetch_a = await make_fetch("a")
    fetch_b = await make_fetch("b")

    await cache.get_cached_editor_state(key=("a", None), fetch_fn=fetch_a, ttl_seconds=1.0)
    await cache.get_cached_editor_state(key=("b", None), fetch_fn=fetch_b, ttl_seconds=1.0)
    await cache.get_cached_editor_state(key=("a", None), fetch_fn=fetch_a, ttl_seconds=1.0)

    assert calls == {"a": 1, "b": 1}


@pytest.mark.asyncio
async def test_invalidate_removes_specific_key():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"success": True, "data": {"value": "x"}}

    key = ("inst-a", None)

    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=10.0)
    cache.invalidate(key)
    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=10.0)

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_pydantic_like_payload_is_cached():
    """Cache must accept any object with a .success attribute, not just dicts."""

    class FakeModel:
        def __init__(self, success: bool):
            self.success = success

    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return FakeModel(success=True)

    key = ("inst-a", None)

    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)
    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=1.0)

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_ttl_expiry_triggers_refetch():
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return {"success": True, "data": {"call": calls["n"]}}

    key = ("inst-a", None)

    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=0.05)
    await asyncio.sleep(0.08)
    await cache.get_cached_editor_state(key=key, fetch_fn=fetch, ttl_seconds=0.05)

    assert calls["n"] == 2
