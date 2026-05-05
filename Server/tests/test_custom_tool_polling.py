"""Tests for CustomToolService._poll_until_complete after migration to wait_for_async_job.

Covers the three behavioral contracts that must survive the refactor:
1. Returns the terminal response when polling completes.
2. Synthesizes a "pending" response on transient transport errors and keeps polling.
3. Returns a Timeout MCPResponse when the deadline passes (NOT the last response).
"""
from __future__ import annotations

import pytest

import services.custom_tool_service as cts


class _FakeAsyncSendMixin:
    """Patch send_with_unity_instance to drive a sequence of fake responses."""

    @staticmethod
    def patch(monkeypatch, sequence):
        state = {"calls": 0}

        async def fake_send(send_fn, unity_instance, tool_name, params, **kwargs):
            idx = min(state["calls"], len(sequence) - 1)
            state["calls"] += 1
            entry = sequence[idx]
            if isinstance(entry, Exception):
                raise entry
            return entry

        monkeypatch.setattr(cts, "send_with_unity_instance", fake_send)
        return state


@pytest.mark.asyncio
async def test_poll_until_complete_returns_terminal(monkeypatch):
    state = _FakeAsyncSendMixin.patch(
        monkeypatch,
        [
            {"_mcp_status": "pending", "_mcp_poll_interval": 0.05},
            {"_mcp_status": "pending", "_mcp_poll_interval": 0.05},
            {"success": True, "data": {"value": 42}},  # terminal (no _mcp_status)
        ],
    )

    service = cts.CustomToolService.__new__(cts.CustomToolService)
    initial = {"_mcp_status": "pending", "_mcp_poll_interval": 0.05}

    result = await service._poll_until_complete(
        tool_name="my_tool",
        unity_instance="unity-1",
        initial_params={"action": "do_thing"},
        initial_response=initial,
        poll_action="status",
        max_poll_seconds=30,
    )

    assert result.success is True
    assert result.data == {"value": 42}
    # Three calls: kickoff is the supplied initial_response, but the helper
    # itself fetches fresh, so it makes one fetch per pending tick + the
    # terminal one.
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_poll_until_complete_short_circuits_when_initial_terminal(monkeypatch):
    """Tools that return success immediately should not enter the wait loop at all."""
    state = _FakeAsyncSendMixin.patch(monkeypatch, [{"success": True}])

    service = cts.CustomToolService.__new__(cts.CustomToolService)
    initial = {"success": True, "data": {"already": "done"}}

    result = await service._poll_until_complete(
        tool_name="my_tool",
        unity_instance="unity-1",
        initial_params={"action": "do_thing"},
        initial_response=initial,
        poll_action="status",
        max_poll_seconds=30,
    )

    assert result.success is True
    assert result.data == {"already": "done"}
    assert state["calls"] == 0  # never polled


@pytest.mark.asyncio
async def test_poll_until_complete_returns_timeout_response(monkeypatch):
    """When deadline passes, return MCPResponse(success=False) with the last data."""
    _FakeAsyncSendMixin.patch(
        monkeypatch,
        [{"_mcp_status": "pending", "_mcp_poll_interval": 0.05,
          "data": {"phase": "still working"}}],
    )

    service = cts.CustomToolService.__new__(cts.CustomToolService)
    initial = {"_mcp_status": "pending", "_mcp_poll_interval": 0.05}

    result = await service._poll_until_complete(
        tool_name="my_tool",
        unity_instance="unity-1",
        initial_params={"action": "do_thing"},
        initial_response=initial,
        poll_action="status",
        max_poll_seconds=1,
    )

    assert result.success is False
    assert "Timeout waiting for my_tool" in result.message
    # The last response payload must be preserved in data so callers can act on it.
    assert result.data is not None


@pytest.mark.asyncio
async def test_poll_until_complete_recovers_from_transient_error(monkeypatch):
    """A raise inside send_with_unity_instance must NOT bubble up — synthesize
    a pending response and continue polling."""
    state = _FakeAsyncSendMixin.patch(
        monkeypatch,
        [
            RuntimeError("WebSocket reset by peer"),  # first poll fails
            {"_mcp_status": "pending", "_mcp_poll_interval": 0.05},
            {"success": True, "data": {"recovered": True}},
        ],
    )

    service = cts.CustomToolService.__new__(cts.CustomToolService)
    initial = {"_mcp_status": "pending", "_mcp_poll_interval": 0.05}

    result = await service._poll_until_complete(
        tool_name="my_tool",
        unity_instance="unity-1",
        initial_params={"action": "do_thing"},
        initial_response=initial,
        poll_action="status",
        max_poll_seconds=30,
    )

    assert result.success is True
    assert result.data == {"recovered": True}
    assert state["calls"] == 3
