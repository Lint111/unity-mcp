"""Short-lived TTL + single-flight cache around editor_state.get_editor_state.

When several tools fire in quick succession (a common pattern: a client batches
manage_gameobject + manage_components + read_console), each one independently
calls preflight() which previously round-tripped get_editor_state to Unity.
That serialised burst of identical reads adds noticeable latency and editor
chatter.

This cache wraps the fetch with two cheap optimisations:

1. **TTL** — successful results are reused for `ttl_seconds` (default 0.25s)
   per (unity_instance, user_id) key. Failures are not cached.
2. **Single-flight** — concurrent calls with the same key share one in-flight
   future, so a parallel fan-out only triggers one round-trip.

Negative results (busy/error responses) are not cached — they should be
re-evaluated on the next call so transient busy windows don't get pinned.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# (key, value) entries are protected by `_lock`. The lock is short-held: only
# during dictionary mutation, never across an await on the upstream fetch.
_lock = asyncio.Lock()


@dataclass(slots=True)
class _Entry:
    expires_at: float
    payload: Any


_cache: dict[tuple[str | None, str | None], _Entry] = {}
_inflight: dict[tuple[str | None, str | None], asyncio.Future] = {}

# Keep the API surface small: callers pass a fetch_fn so the cache stays
# decoupled from editor_state import cycles.
FetchFn = Callable[[], Awaitable[Any]]


def _is_cacheable(payload: Any) -> bool:
    # Only cache successful responses. Pydantic models expose .success;
    # dict responses follow the same convention.
    if hasattr(payload, "success"):
        return bool(payload.success)
    if isinstance(payload, dict):
        return bool(payload.get("success", False))
    return False


async def get_cached_editor_state(
    *,
    key: tuple[str | None, str | None],
    fetch_fn: FetchFn,
    ttl_seconds: float = 0.25,
) -> Any:
    """Return a cached editor-state payload for `key`, fetching if missing/expired.

    `key` is typically (unity_instance, user_id). Pass (None, None) when there
    is only one Unity in play and no per-user isolation.
    """
    now = time.monotonic()

    async with _lock:
        entry = _cache.get(key)
        if entry is not None and entry.expires_at > now:
            return entry.payload

        future = _inflight.get(key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            _inflight[key] = future
            owns_fetch = True
        else:
            owns_fetch = False

    if not owns_fetch:
        return await future

    try:
        payload = await fetch_fn()
    except BaseException as exc:  # noqa: BLE001 — propagate after cleanup
        async with _lock:
            _inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
        raise

    async with _lock:
        _inflight.pop(key, None)
        if _is_cacheable(payload):
            _cache[key] = _Entry(
                expires_at=time.monotonic() + ttl_seconds,
                payload=payload,
            )
        if not future.done():
            future.set_result(payload)
    return payload


def invalidate(key: tuple[str | None, str | None] | None = None) -> None:
    """Drop a single key, or the entire cache when called with no key.

    Tests use this; tools may invalidate after a write that they know mutates
    editor state (e.g., refresh_unity completion).
    """
    if key is None:
        _cache.clear()
        return
    _cache.pop(key, None)
