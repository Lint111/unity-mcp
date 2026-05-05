"""Tests for manage_build MCP tool."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.tools.manage_build import ALL_ACTIONS, manage_build


@pytest.fixture
def mock_unity(monkeypatch):
    """Patch Unity transport layer and return captured call dict."""
    captured: dict[str, object] = {}

    async def fake_send(send_fn, unity_instance, tool_name, params):
        captured["unity_instance"] = unity_instance
        captured["tool_name"] = tool_name
        captured["params"] = params
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(
        "services.tools.manage_build.get_unity_instance_from_context",
        AsyncMock(return_value="unity-instance-1"),
    )
    monkeypatch.setattr(
        "services.tools.manage_build.send_with_unity_instance",
        fake_send,
    )
    return captured


# ── action validation ───────────────────────────────────────────────

def test_all_actions_count():
    assert len(ALL_ACTIONS) == 8


def test_unknown_action_returns_error(mock_unity):
    result = asyncio.run(manage_build(SimpleNamespace(), action="nonexistent"))
    assert result["success"] is False
    assert "Unknown action" in result["message"]
    assert "tool_name" not in mock_unity


# ── build action ────────────────────────────────────────────────────

def test_build_forwards_params(mock_unity):
    result = asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="build",
            target="windows64",
            development="true",
            output_path="Builds/Win/Game.exe",
            scripting_backend="il2cpp",
        )
    )
    assert result["success"] is True
    params = mock_unity["params"]
    assert params["action"] == "build"
    assert params["target"] == "windows64"
    assert params["development"] is True
    assert params["output_path"] == "Builds/Win/Game.exe"
    assert params["scripting_backend"] == "il2cpp"


def test_build_omits_none_params(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="build"))
    params = mock_unity["params"]
    assert params == {"action": "build"}


def test_build_with_options(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="build",
            options='["clean_build", "auto_run"]',
        )
    )
    params = mock_unity["params"]
    assert params["options"] == ["clean_build", "auto_run"]


def test_build_with_scenes(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="build",
            scenes='["Assets/Scenes/Main.unity", "Assets/Scenes/Level1.unity"]',
        )
    )
    params = mock_unity["params"]
    assert params["scenes"] == ["Assets/Scenes/Main.unity", "Assets/Scenes/Level1.unity"]


# ── status action ──────────────────────────────────────────────────

def test_status_forwards_job_id(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="status", job_id="build-abc123"))
    params = mock_unity["params"]
    assert params["action"] == "status"
    assert params["job_id"] == "build-abc123"


def test_status_without_job_id(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="status"))
    params = mock_unity["params"]
    assert params == {"action": "status"}


# ── platform action ────────────────────────────────────────────────

def test_platform_read(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="platform"))
    params = mock_unity["params"]
    assert params == {"action": "platform"}


def test_platform_switch(mock_unity):
    asyncio.run(
        manage_build(SimpleNamespace(), action="platform", target="android", subtarget="player")
    )
    params = mock_unity["params"]
    assert params["target"] == "android"
    assert params["subtarget"] == "player"


# ── settings action ────────────────────────────────────────────────

def test_settings_read(mock_unity):
    asyncio.run(
        manage_build(SimpleNamespace(), action="settings", property="product_name")
    )
    params = mock_unity["params"]
    assert params["action"] == "settings"
    assert params["property"] == "product_name"
    assert "value" not in params


def test_settings_write(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="settings",
            property="product_name",
            value="My Game",
        )
    )
    params = mock_unity["params"]
    assert params["property"] == "product_name"
    assert params["value"] == "My Game"


# ── scenes action ──────────────────────────────────────────────────

def test_scenes_read(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="scenes"))
    params = mock_unity["params"]
    assert params == {"action": "scenes"}


def test_scenes_write(mock_unity):
    scenes_json = '[{"path": "Assets/Scenes/Main.unity", "enabled": true}]'
    asyncio.run(manage_build(SimpleNamespace(), action="scenes", scenes=scenes_json))
    params = mock_unity["params"]
    assert params["scenes"] == [{"path": "Assets/Scenes/Main.unity", "enabled": True}]


# ── profiles action ────────────────────────────────────────────────

def test_profiles_list(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="profiles"))
    params = mock_unity["params"]
    assert params == {"action": "profiles"}


def test_profiles_activate(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="profiles",
            profile="Assets/Settings/Build Profiles/iOS.asset",
            activate="true",
        )
    )
    params = mock_unity["params"]
    assert params["profile"] == "Assets/Settings/Build Profiles/iOS.asset"
    assert params["activate"] is True


# ── batch action ────────────────────────────────────────────────────

def test_batch_with_targets(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="batch",
            targets='["windows64", "linux64", "webgl"]',
            development="true",
        )
    )
    params = mock_unity["params"]
    assert params["action"] == "batch"
    assert params["targets"] == ["windows64", "linux64", "webgl"]
    assert params["development"] is True


def test_batch_with_profiles(mock_unity):
    asyncio.run(
        manage_build(
            SimpleNamespace(),
            action="batch",
            profiles='["Assets/Profiles/A.asset", "Assets/Profiles/B.asset"]',
        )
    )
    params = mock_unity["params"]
    assert params["profiles"] == ["Assets/Profiles/A.asset", "Assets/Profiles/B.asset"]


# ── cancel action ──────────────────────────────────────────────────

def test_cancel_forwards_job_id(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="cancel", job_id="batch-xyz789"))
    params = mock_unity["params"]
    assert params["action"] == "cancel"
    assert params["job_id"] == "batch-xyz789"


# ── minimal param forwarding ────────────────────────────────────────

def test_batch_without_targets_sends_minimal_params(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="batch"))
    params = mock_unity["params"]
    assert params == {"action": "batch"}


def test_settings_without_property_sends_minimal_params(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="settings"))
    params = mock_unity["params"]
    assert params == {"action": "settings"}


def test_cancel_without_job_id_sends_minimal_params(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="cancel"))
    params = mock_unity["params"]
    assert params == {"action": "cancel"}


# ── transport ───────────────────────────────────────────────────────

def test_sends_to_correct_tool_name(mock_unity):
    asyncio.run(manage_build(SimpleNamespace(), action="status"))
    assert mock_unity["tool_name"] == "manage_build"


# ── wait_timeout long-poll behavior ─────────────────────────────────


def _wait_mock(monkeypatch, response_sequence):
    """Replace transport with a sequence-driven fake. Returns the call counter."""
    state = {"calls": 0, "params_log": []}

    async def fake_send(send_fn, unity_instance, tool_name, params):
        state["params_log"].append(dict(params))
        idx = min(state["calls"], len(response_sequence) - 1)
        state["calls"] += 1
        return response_sequence[idx]

    monkeypatch.setattr(
        "services.tools.manage_build.get_unity_instance_from_context",
        AsyncMock(return_value="unity-instance-1"),
    )
    monkeypatch.setattr(
        "services.tools.manage_build.send_with_unity_instance",
        fake_send,
    )
    return state


@pytest.mark.asyncio
async def test_build_wait_timeout_returns_terminal_response(monkeypatch):
    state = _wait_mock(
        monkeypatch,
        [
            {"_mcp_status": "pending", "data": {"job_id": "build-1"}},
            {"_mcp_status": "pending", "data": {"job_id": "build-1"}},
            {"success": True, "data": {"status": "succeeded", "job_id": "build-1"}},
        ],
    )

    result = await manage_build(
        SimpleNamespace(), action="build", target="windows64", wait_timeout=5
    )

    # Returns the terminal (non-pending) payload, not the initial pending one.
    assert result.get("_mcp_status") != "pending"
    assert result["data"]["status"] == "succeeded"
    # First call is the kickoff (action=build); subsequent calls are status polls.
    assert state["params_log"][0]["action"] == "build"
    assert state["params_log"][1]["action"] == "status"
    assert state["params_log"][1]["job_id"] == "build-1"
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_build_wait_timeout_returns_pending_on_timeout(monkeypatch):
    _wait_mock(
        monkeypatch,
        [{"_mcp_status": "pending", "data": {"job_id": "build-2", "phase": "compiling"}}],
    )

    result = await manage_build(
        SimpleNamespace(), action="build", target="windows64", wait_timeout=1
    )

    assert result["_mcp_status"] == "pending"
    assert result["data"]["phase"] == "compiling"


@pytest.mark.asyncio
async def test_build_wait_timeout_skipped_when_response_already_terminal(monkeypatch):
    state = _wait_mock(
        monkeypatch,
        [{"success": True, "message": "Build profile activated.", "data": {}}],
    )

    result = await manage_build(
        SimpleNamespace(), action="profiles", activate="true",
        profile="Assets/profile.asset", wait_timeout=30,
    )

    # Single round-trip — no status polls because nothing is pending.
    assert state["calls"] == 1
    assert result["success"] is True


@pytest.mark.asyncio
async def test_build_wait_timeout_zero_skips_wait_loop(monkeypatch):
    state = _wait_mock(
        monkeypatch,
        [{"_mcp_status": "pending", "data": {"job_id": "build-3"}}],
    )

    result = await manage_build(
        SimpleNamespace(), action="build", target="windows64", wait_timeout=0
    )

    assert state["calls"] == 1  # no polling loop
    assert result["_mcp_status"] == "pending"


@pytest.mark.asyncio
async def test_build_wait_timeout_negative_returns_validation_error(monkeypatch):
    _wait_mock(monkeypatch, [{"success": True}])

    result = await manage_build(
        SimpleNamespace(), action="build", target="windows64", wait_timeout=-5
    )

    assert result["success"] is False
    assert "wait_timeout" in result["message"]


@pytest.mark.asyncio
async def test_build_wait_timeout_uses_provided_job_id_when_no_data(monkeypatch):
    """If kickoff payload has no data.job_id but caller provided one, use it."""
    state = _wait_mock(
        monkeypatch,
        [
            {"_mcp_status": "pending"},  # no data.job_id
            {"success": True, "data": {"status": "succeeded"}},
        ],
    )

    result = await manage_build(
        SimpleNamespace(), action="status", job_id="build-existing", wait_timeout=5
    )

    assert state["params_log"][0]["action"] == "status"
    assert state["params_log"][1]["job_id"] == "build-existing"
    assert result["data"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_build_wait_timeout_returns_pending_when_no_addressable_job(monkeypatch):
    """If the kickoff is pending but no job_id is available anywhere, return as-is."""
    state = _wait_mock(
        monkeypatch, [{"_mcp_status": "pending", "data": "not-a-dict"}],
    )

    result = await manage_build(
        SimpleNamespace(), action="build", target="windows64", wait_timeout=5
    )

    assert state["calls"] == 1  # cannot poll without a job_id
    assert result["_mcp_status"] == "pending"


@pytest.mark.asyncio
async def test_build_wait_timeout_cancels_cleanly(monkeypatch):
    _wait_mock(monkeypatch, [{"_mcp_status": "pending", "data": {"job_id": "x"}}])

    task = asyncio.create_task(
        manage_build(
            SimpleNamespace(), action="build", target="windows64", wait_timeout=60
        )
    )
    await asyncio.sleep(0.1)  # let it enter the wait loop
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
