"""Unit tests for services.tools._wait.wait_for_async_job."""

import asyncio

import pytest

from services.tools._wait import wait_for_async_job


# ── single-shot path ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_returns_immediately_when_first_response_is_terminal():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"success": True, "message": "done"}  # no _mcp_status → terminal

    result = await wait_for_async_job(fetch_status=fetch, timeout_seconds=10)
    assert result == {"success": True, "message": "done"}
    assert calls == 1


@pytest.mark.asyncio
async def test_returns_immediately_when_timeout_is_zero():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"_mcp_status": "pending"}

    result = await wait_for_async_job(fetch_status=fetch, timeout_seconds=0)
    assert result == {"_mcp_status": "pending"}
    assert calls == 1


@pytest.mark.asyncio
async def test_returns_immediately_when_timeout_is_none():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        return {"_mcp_status": "pending"}

    result = await wait_for_async_job(fetch_status=fetch, timeout_seconds=None)
    assert calls == 1
    assert result["_mcp_status"] == "pending"


# ── polling until terminal ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_polls_until_terminal():
    sequence = [
        {"_mcp_status": "pending"},
        {"_mcp_status": "pending"},
        {"success": True, "data": {"status": "succeeded"}},
    ]
    calls = 0

    async def fetch():
        nonlocal calls
        result = sequence[min(calls, len(sequence) - 1)]
        calls += 1
        return result

    result = await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=5,
        initial_interval=0.01,
        max_interval=0.05,
    )
    assert result == {"success": True, "data": {"status": "succeeded"}}
    assert calls == 3


@pytest.mark.asyncio
async def test_returns_last_response_on_timeout():
    async def fetch():
        return {"_mcp_status": "pending", "data": {"phase": "compiling"}}

    result = await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=1,
        initial_interval=0.01,
        max_interval=0.05,
    )
    # Returns the last pending payload, NOT an exception.
    assert result["_mcp_status"] == "pending"
    assert result["data"] == {"phase": "compiling"}


# ── adaptive backoff behavior ───────────────────────────────────────

@pytest.mark.asyncio
async def test_backoff_doubles_when_no_progress(monkeypatch):
    sleeps: list[float] = []

    async def recorded_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("services.tools._wait.asyncio.sleep", recorded_sleep)

    sequence = [{"_mcp_status": "pending"}] * 5 + [{"success": True}]
    idx = 0

    async def fetch():
        nonlocal idx
        result = sequence[idx]
        idx += 1
        return result

    await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=300,
        initial_interval=0.1,
        max_interval=0.8,
    )

    # First sleep is initial_interval; each subsequent doubles up to cap.
    assert len(sleeps) >= 4
    assert pytest.approx(sleeps[0], rel=0.01) == 0.1
    assert pytest.approx(sleeps[1], rel=0.01) == 0.2
    assert pytest.approx(sleeps[2], rel=0.01) == 0.4
    assert pytest.approx(sleeps[3], rel=0.01) == 0.8
    # Capped — does not exceed max_interval.
    assert all(s <= 0.8 + 1e-6 for s in sleeps)


@pytest.mark.asyncio
async def test_backoff_resets_when_progress_observed(monkeypatch):
    sleeps: list[float] = []

    async def recorded_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("services.tools._wait.asyncio.sleep", recorded_sleep)

    sequence = [
        {"_mcp_status": "pending", "data": {"completed": 0}},  # initial fetch
        {"_mcp_status": "pending", "data": {"completed": 0}},  # no progress → 0.1
        {"_mcp_status": "pending", "data": {"completed": 0}},  # no progress → 0.2
        {"_mcp_status": "pending", "data": {"completed": 1}},  # progress! → reset to 0.1
        {"_mcp_status": "pending", "data": {"completed": 1}},  # no progress → 0.2
        {"success": True},
    ]
    idx = 0

    async def fetch():
        nonlocal idx
        result = sequence[idx]
        idx += 1
        return result

    await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=300,
        initial_interval=0.1,
        max_interval=1.0,
        progress_key=lambda r: (r.get("data") or {}).get("completed"),
    )

    assert pytest.approx(sleeps[0], rel=0.01) == 0.1  # before 2nd fetch
    assert pytest.approx(sleeps[1], rel=0.01) == 0.2  # before 3rd fetch (no progress)
    assert pytest.approx(sleeps[2], rel=0.01) == 0.4  # before 4th fetch (still no progress)
    assert pytest.approx(sleeps[3], rel=0.01) == 0.1  # before 5th — progress just reset


# ── cancellation ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancellation_propagates_cleanly():
    started = asyncio.Event()

    async def fetch():
        started.set()
        return {"_mcp_status": "pending"}

    task = asyncio.create_task(
        wait_for_async_job(
            fetch_status=fetch,
            timeout_seconds=60,
            initial_interval=0.05,
            max_interval=0.5,
        )
    )

    await started.wait()
    await asyncio.sleep(0.06)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ── on_each_response callback ───────────────────────────────────────

@pytest.mark.asyncio
async def test_on_each_response_invoked_for_every_fetch():
    sequence = [{"_mcp_status": "pending"}, {"success": True}]
    idx = 0
    seen: list[dict] = []

    async def fetch():
        nonlocal idx
        result = sequence[idx]
        idx += 1
        return result

    async def cb(resp):
        seen.append(resp)

    await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=5,
        initial_interval=0.01,
        max_interval=0.05,
        on_each_response=cb,
    )

    assert seen == sequence


@pytest.mark.asyncio
async def test_on_each_response_callback_exception_is_swallowed():
    """Callback errors must not break the wait loop."""
    sequence = [{"_mcp_status": "pending"}, {"success": True}]
    idx = 0

    async def fetch():
        nonlocal idx
        result = sequence[idx]
        idx += 1
        return result

    async def bad_cb(_resp):
        raise RuntimeError("telemetry exploded")

    # Must not raise; helper should still return the terminal response.
    result = await wait_for_async_job(
        fetch_status=fetch,
        timeout_seconds=5,
        initial_interval=0.01,
        max_interval=0.05,
        on_each_response=bad_cb,
    )
    assert result == {"success": True}
