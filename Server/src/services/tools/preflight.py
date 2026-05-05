from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from models import MCPResponse
from services.state.editor_state_cache import get_cached_editor_state

# TTL for the preflight editor-state cache. Short enough that a busy → idle
# transition is picked up within one tick of the wait loop, long enough to
# coalesce a typical client burst (several tools dispatched in the same render
# frame).
_PREFLIGHT_TTL_S = 0.25


def _in_pytest() -> bool:
    # Integration tests in this repo stub transports and do not run against a live Unity editor.
    # Preflight must be a no-op in that environment to avoid breaking the existing test suite.
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _busy(reason: str, retry_after_ms: int) -> MCPResponse:
    return MCPResponse(
        success=False,
        error="busy",
        message=reason,
        hint="retry",
        data={"reason": reason, "retry_after_ms": int(retry_after_ms)},
    )


async def _resolve_cache_key(ctx) -> tuple[str | None, str | None]:
    """Build the (unity_instance, user_id) key used by the editor-state cache."""
    try:
        from services.tools import get_unity_instance_from_context
        unity_instance = await get_unity_instance_from_context(ctx)
    except Exception:
        unity_instance = None
    user_id = None
    try:
        # Best-effort user_id extraction for remote-hosted mode; falls back to None.
        from transport.unity_transport import _resolve_user_id_from_request
        user_id = await _resolve_user_id_from_request()
    except Exception:
        user_id = None
    return (unity_instance, user_id)


async def _fetch_editor_state(ctx):
    from services.resources.editor_state import get_editor_state
    return await get_editor_state(ctx)


async def _load_state(ctx) -> dict | None:
    """Return the editor_state payload as a dict, served from cache when fresh."""
    key = await _resolve_cache_key(ctx)
    state_resp = await get_cached_editor_state(
        key=key,
        fetch_fn=lambda: _fetch_editor_state(ctx),
        ttl_seconds=_PREFLIGHT_TTL_S,
    )
    state = state_resp.model_dump() if hasattr(state_resp, "model_dump") else state_resp
    return state if isinstance(state, dict) else None


async def preflight(
    ctx,
    *,
    requires_no_tests: bool = False,
    wait_for_no_compile: bool = False,
    refresh_if_dirty: bool = False,
    max_wait_s: float = 30.0,
) -> MCPResponse | None:
    """
    Server-side preflight guard used by tools so they behave safely even if the client never reads resources.

    Returns:
      - MCPResponse busy/retry payload when the tool should not proceed right now
      - None when the tool should proceed normally
    """
    if _in_pytest():
        return None

    # Load canonical editor state (server enriches advice + staleness).
    try:
        state = await _load_state(ctx)
    except Exception:
        # If we cannot determine readiness, fall back to proceeding (tools already contain retry logic).
        return None

    if state is None or not state.get("success", False):
        # Unknown state; proceed rather than blocking (avoids false positives when Unity is reachable but status isn't).
        return None

    data = state.get("data")
    if not isinstance(data, dict):
        return None

    # Optional refresh-if-dirty
    if refresh_if_dirty:
        assets = data.get("assets")
        if isinstance(assets, dict) and assets.get("external_changes_dirty") is True:
            try:
                from services.tools.refresh_unity import refresh_unity
                await refresh_unity(ctx, mode="if_dirty", scope="all", compile="request", wait_for_ready=True)
            except Exception:
                # Best-effort only; fall through to normal tool dispatch.
                pass

    # Tests running: fail fast for tools that require exclusivity.
    if requires_no_tests:
        tests = data.get("tests")
        if isinstance(tests, dict) and tests.get("is_running") is True:
            return _busy("tests_running", 5000)

    # Compilation: optionally wait for a bounded time.
    if wait_for_no_compile:
        deadline = time.monotonic() + float(max_wait_s)
        # Adaptive sleep: short ticks at first so a fast settle returns quickly,
        # then double up to a 500ms cap so a long compilation doesn't churn.
        sleep_s = 0.05
        while True:
            compilation = data.get("compilation") if isinstance(
                data, dict) else None
            is_compiling = isinstance(compilation, dict) and compilation.get(
                "is_compiling") is True
            is_domain_reload_pending = isinstance(compilation, dict) and compilation.get(
                "is_domain_reload_pending") is True
            if not is_compiling and not is_domain_reload_pending:
                break
            if time.monotonic() >= deadline:
                return _busy("compiling", 500)
            await asyncio.sleep(sleep_s)
            sleep_s = min(0.5, sleep_s * 2)

            # Refresh state for the next loop iteration. The cache TTL guarantees
            # we hit Unity at most once per ~250ms even when sleep_s is shorter.
            try:
                state = await _load_state(ctx)
                data = state.get("data") if isinstance(state, dict) else None
                if not isinstance(data, dict):
                    return None
            except Exception:
                return None

    # Staleness: if the snapshot is stale, proceed (tools will still run), but callers that read resources can back off.
    # In future we may make this strict for some tools.
    return None
