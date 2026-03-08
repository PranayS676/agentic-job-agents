from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import job_agent_runtime.main as main_module


@pytest.mark.asyncio
async def test_run_runtime_handles_task_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = SimpleNamespace(log_level="INFO")
    state = {"watcher_started": False, "watcher_cancelled": False}

    class _FakeWatcher:
        def __init__(self, settings=None):  # noqa: ANN001
            _ = settings

        async def run_forever(self, pipeline_runner):  # noqa: ANN001
            state["watcher_started"] = True
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                state["watcher_cancelled"] = True
                raise

    class _FakeRunner:
        def __init__(self, settings=None):  # noqa: ANN001
            _ = settings

    monkeypatch.setattr(main_module, "WatcherService", _FakeWatcher)
    monkeypatch.setattr(main_module, "ManagerPipelineRunner", _FakeRunner)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: callback())  # noqa: ARG005

    exit_code = await main_module._run_runtime(
        settings=settings,
        log_level_override=None,
    )

    assert exit_code == 0
    assert state["watcher_started"] is True
    assert state["watcher_cancelled"] is True

