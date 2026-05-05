"""Server-side long-poll helper.

A single tool call can wait up to ``timeout_seconds`` for a Unity-side async
job to reach a terminal state, instead of forcing the agent to issue its own
poll loop. Adaptive backoff (250 ms → ``max_interval``) keeps the cadence
tight on fast-completing jobs and loose on slow ones; observed forward
progress resets the backoff.

Cancellation: the helper uses ``await asyncio.sleep(...)`` between polls, so
cancelling the surrounding MCP request propagates ``asyncio.CancelledError``
naturally. Callers that own a Unity-side cancel (e.g. ``manage_build
action=cancel``) should issue it from their own ``finally``.

This is the canonical long-poll path; ``run_tests.get_test_job`` predates
it and will be migrated separately.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _default_is_terminal(response: Any) -> bool:
    """A response is terminal unless the C# side flagged it ``pending``."""
    return not (
        isinstance(response, dict) and response.get("_mcp_status") == "pending"
    )


async def wait_for_async_job(
    *,
    fetch_status: Callable[[], Awaitable[Any]],
    timeout_seconds: int,
    is_terminal: Callable[[Any], bool] = _default_is_terminal,
    initial_interval: float = 0.25,
    max_interval: float = 2.0,
    progress_key: Callable[[Any], Any] | None = None,
    on_each_response: Optional[Callable[[Any], Awaitable[None]]] = None,
) -> Any:
    """Poll ``fetch_status`` until terminal or the deadline passes.

    Parameters
    ----------
    fetch_status:
        Coroutine returning the current Unity response (typically a dict).
    timeout_seconds:
        Maximum wall-clock seconds to wait. ``<= 0`` means "single shot":
        call ``fetch_status`` once and return.
    is_terminal:
        Predicate that returns True when polling should stop. Defaults to
        "anything that isn't ``_mcp_status=pending``".
    initial_interval, max_interval:
        Backoff bounds. The first sleep is ``initial_interval``; each
        no-progress tick doubles the interval up to ``max_interval``.
    progress_key:
        Optional extractor that returns a comparable value (e.g. a Unix-ms
        timestamp, or completed-test count). When the returned value differs
        between consecutive polls, the backoff resets to ``initial_interval``
        — fast jobs that are still moving keep getting polled often, while
        truly idle jobs back off.
    on_each_response:
        Optional async callback invoked on every fetched response (terminal
        or not). Useful for telemetry, focus nudges, etc. Exceptions inside
        the callback are logged and swallowed.

    Returns
    -------
    The most recent response. Callers wrap it into their own response model.
    """
    response = await fetch_status()
    if on_each_response is not None:
        await _safe_call(on_each_response, response)

    if timeout_seconds is None or timeout_seconds <= 0 or is_terminal(response):
        return response

    deadline = asyncio.get_event_loop().time() + timeout_seconds
    interval = max(0.05, initial_interval)
    cap = max(interval, max_interval)
    prev_progress = progress_key(response) if progress_key else None

    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return response  # last known state on timeout

        await asyncio.sleep(min(interval, remaining))

        response = await fetch_status()
        if on_each_response is not None:
            await _safe_call(on_each_response, response)

        if is_terminal(response):
            return response

        if progress_key is not None:
            current = progress_key(response)
            if current != prev_progress:
                interval = max(0.05, initial_interval)  # progress: tighten
            else:
                interval = min(cap, interval * 2)        # idle: loosen
            prev_progress = current
        else:
            interval = min(cap, interval * 2)


async def _safe_call(cb: Callable[[Any], Awaitable[None]], response: Any) -> None:
    try:
        await cb(response)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("wait_for_async_job on_each_response callback failed",
                     exc_info=True)
