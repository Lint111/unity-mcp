import pytest

from .test_helpers import DummyContext


@pytest.mark.asyncio
async def test_run_tests_async_forwards_params(monkeypatch):
    from services.tools.run_tests import run_tests

    captured = {}

    async def fake_send_with_unity_instance(send_fn, unity_instance, command_type, params, **kwargs):
        captured["command_type"] = command_type
        captured["params"] = params
        return {"success": True, "data": {"job_id": "abc123", "status": "running", "mode": "EditMode"}}

    import services.tools.run_tests as mod
    monkeypatch.setattr(
        mod.unity_transport, "send_with_unity_instance", fake_send_with_unity_instance)

    resp = await run_tests(
        DummyContext(),
        mode="EditMode",
        test_names="MyNamespace.MyTests.TestA",
        include_details=True,
    )
    assert captured["command_type"] == "run_tests"
    assert captured["params"]["mode"] == "EditMode"
    assert captured["params"]["testNames"] == ["MyNamespace.MyTests.TestA"]
    assert captured["params"]["includeDetails"] is True
    assert resp.success is True
    assert resp.data is not None
    assert resp.data.job_id == "abc123"


@pytest.mark.asyncio
async def test_run_tests_forwards_init_timeout(monkeypatch):
    from services.tools.run_tests import run_tests

    captured = {}

    async def fake_send_with_unity_instance(send_fn, unity_instance, command_type, params, **kwargs):
        captured["params"] = params
        return {"success": True, "data": {"job_id": "abc123", "status": "running", "mode": "PlayMode"}}

    import services.tools.run_tests as mod
    monkeypatch.setattr(
        mod.unity_transport, "send_with_unity_instance", fake_send_with_unity_instance)

    resp = await run_tests(
        DummyContext(),
        mode="PlayMode",
        init_timeout=120000,
    )
    assert captured["params"]["initTimeout"] == 120000
    assert resp.success is True


@pytest.mark.asyncio
async def test_run_tests_omits_init_timeout_when_none(monkeypatch):
    from services.tools.run_tests import run_tests

    captured = {}

    async def fake_send_with_unity_instance(send_fn, unity_instance, command_type, params, **kwargs):
        captured["params"] = params
        return {"success": True, "data": {"job_id": "abc123", "status": "running", "mode": "EditMode"}}

    import services.tools.run_tests as mod
    monkeypatch.setattr(
        mod.unity_transport, "send_with_unity_instance", fake_send_with_unity_instance)

    resp = await run_tests(DummyContext(), mode="EditMode")
    assert "initTimeout" not in captured["params"]
    assert resp.success is True


@pytest.mark.asyncio
async def test_run_tests_rejects_negative_init_timeout():
    from services.tools.run_tests import run_tests

    resp = await run_tests(DummyContext(), mode="EditMode", init_timeout=-1)
    assert resp.success is False
    assert "init_timeout" in resp.error


@pytest.mark.asyncio
async def test_run_tests_rejects_zero_init_timeout():
    from services.tools.run_tests import run_tests

    resp = await run_tests(DummyContext(), mode="EditMode", init_timeout=0)
    assert resp.success is False
    assert "init_timeout" in resp.error


@pytest.mark.asyncio
async def test_get_test_job_forwards_job_id(monkeypatch):
    from services.tools.run_tests import get_test_job

    captured = {}

    async def fake_send_with_unity_instance(send_fn, unity_instance, command_type, params, **kwargs):
        captured["command_type"] = command_type
        captured["params"] = params
        return {"success": True, "data": {"job_id": params["job_id"], "status": "running", "mode": "EditMode"}}

    import services.tools.run_tests as mod
    monkeypatch.setattr(
        mod.unity_transport, "send_with_unity_instance", fake_send_with_unity_instance)

    resp = await get_test_job(DummyContext(), job_id="job-1")
    assert captured["command_type"] == "get_test_job"
    assert captured["params"]["job_id"] == "job-1"
    assert resp.success is True
    assert resp.data is not None
    assert resp.data.job_id == "job-1"


@pytest.mark.asyncio
async def test_get_test_job_wait_timeout_returns_terminal(monkeypatch):
    """wait_timeout polls until data.status is succeeded/failed/cancelled."""
    from services.tools.run_tests import get_test_job
    import services.tools.run_tests as mod

    sequence = [
        {"success": True, "data": {"job_id": "j", "status": "running",
                                   "last_update_unix_ms": 1, "progress": {"editor_is_focused": True}}},
        {"success": True, "data": {"job_id": "j", "status": "running",
                                   "last_update_unix_ms": 2, "progress": {"editor_is_focused": True}}},
        {"success": True, "data": {"job_id": "j", "status": "succeeded",
                                   "last_update_unix_ms": 3, "progress": {"editor_is_focused": True}}},
    ]
    calls = {"n": 0}

    async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[idx]

    monkeypatch.setattr(mod.unity_transport, "send_with_unity_instance", fake_send)
    monkeypatch.setattr(mod, "_get_unity_project_path", AsyncMock_returning(None))

    resp = await get_test_job(DummyContext(), job_id="j", wait_timeout=10)
    assert resp.success is True
    assert resp.data.status == "succeeded"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_get_test_job_wait_timeout_returns_running_on_timeout(monkeypatch):
    """When the deadline passes, the helper returns the last running snapshot."""
    from services.tools.run_tests import get_test_job
    import services.tools.run_tests as mod

    async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
        return {"success": True, "data": {"job_id": "j", "status": "running",
                                          "last_update_unix_ms": 1,
                                          "progress": {"editor_is_focused": True}}}

    monkeypatch.setattr(mod.unity_transport, "send_with_unity_instance", fake_send)
    monkeypatch.setattr(mod, "_get_unity_project_path", AsyncMock_returning(None))

    resp = await get_test_job(DummyContext(), job_id="j", wait_timeout=1)
    assert resp.success is True
    assert resp.data.status == "running"


@pytest.mark.asyncio
async def test_get_test_job_wait_timeout_short_circuits_on_unsuccessful_response(monkeypatch):
    """A non-success response (e.g., transport error wrapper) terminates the wait."""
    from services.tools.run_tests import get_test_job
    import services.tools.run_tests as mod

    async def fake_send(send_fn, unity_instance, command_type, params, **kwargs):
        return {"success": False, "error": "Unity disconnected"}

    monkeypatch.setattr(mod.unity_transport, "send_with_unity_instance", fake_send)
    monkeypatch.setattr(mod, "_get_unity_project_path", AsyncMock_returning(None))

    resp = await get_test_job(DummyContext(), job_id="j", wait_timeout=10)
    assert resp.success is False
    assert "Unity disconnected" in (resp.error or "")


def AsyncMock_returning(value):
    async def _coro(*args, **kwargs):
        return value
    return _coro
