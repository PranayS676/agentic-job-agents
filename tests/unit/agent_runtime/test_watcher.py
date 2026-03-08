from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from unittest.mock import AsyncMock

from job_agent_runtime.worker.watcher import WatcherService


class _DummySessionContext:
    def __init__(self, session) -> None:
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ARG002
        return False


@dataclass
class _Message:
    id: UUID
    created_at: int


class _Runner:
    def __init__(self, fail_ids: set[UUID] | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.calls: list[tuple[UUID, UUID]] = []

    async def run(self, message, trace_id: UUID) -> dict:  # noqa: ANN001
        self.calls.append((message.id, trace_id))
        if message.id in self.fail_ids:
            raise RuntimeError("forced-runner-failure")
        return {"ok": True}


class _InMemoryWatcher(WatcherService):
    def __init__(self, messages: list[_Message], *, poll_interval_seconds: int = 30) -> None:
        settings = SimpleNamespace(poll_interval_seconds=poll_interval_seconds)
        session = SimpleNamespace(commit=AsyncMock())
        super().__init__(settings=settings, session_factory=lambda: _DummySessionContext(session))
        self._messages = messages
        self.success_ids: list[UUID] = []
        self.failure_by_id: dict[UUID, str] = {}
        self.get_pending_limit_calls: list[int] = []

    async def _get_pending_messages(self, session, limit: int):  # noqa: ANN001
        self.get_pending_limit_calls.append(limit)
        return self._messages[:limit]

    async def _claim_message(self, session, message_id: UUID):  # noqa: ANN001
        return True

    async def _create_pipeline_run(self, session, message_id: UUID):  # noqa: ANN001
        return uuid4()

    async def _mark_message_success(self, session, message_id: UUID):  # noqa: ANN001
        self.success_ids.append(message_id)

    async def _mark_message_failure(self, session, message_id: UUID, error_message: str):  # noqa: ANN001
        self.failure_by_id[message_id] = error_message


@pytest.mark.asyncio
async def test_run_tick_processes_oldest_10_only() -> None:
    messages = [_Message(id=uuid4(), created_at=i) for i in range(12)]
    watcher = _InMemoryWatcher(messages=messages)
    runner = _Runner()

    summary = await watcher.run_tick(pipeline_runner=runner)

    assert summary["processed_count"] == 10
    assert summary["error_count"] == 0
    assert watcher.get_pending_limit_calls == [10]
    assert len(runner.calls) == 10
    assert [msg_id for msg_id, _ in runner.calls] == [msg.id for msg in messages[:10]]


@pytest.mark.asyncio
async def test_run_tick_failure_marks_error_and_continues() -> None:
    messages = [_Message(id=uuid4(), created_at=i) for i in range(3)]
    runner = _Runner(fail_ids={messages[1].id})
    watcher = _InMemoryWatcher(messages=messages)

    summary = await watcher.run_tick(pipeline_runner=runner)

    assert summary["processed_count"] == 2
    assert summary["error_count"] == 1
    assert messages[0].id in watcher.success_ids
    assert messages[2].id in watcher.success_ids
    assert messages[1].id in watcher.failure_by_id
    assert "forced-runner-failure" in watcher.failure_by_id[messages[1].id]


@pytest.mark.asyncio
async def test_run_cleanup_once_issues_delete_query() -> None:
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(rowcount=7)),
        commit=AsyncMock(),
    )
    watcher = WatcherService(
        settings=SimpleNamespace(poll_interval_seconds=30),
        session_factory=lambda: _DummySessionContext(session),
    )

    deleted = await watcher.run_cleanup_once()

    assert deleted == 7
    assert session.execute.await_count == 1
    statement = session.execute.await_args.args[0]
    params = session.execute.await_args.args[1]
    assert "DELETE FROM whatsapp_messages" in str(statement)
    assert "cutoff" in params
    assert session.commit.await_count == 1


@pytest.mark.asyncio
async def test_watch_loop_uses_poll_interval_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher = WatcherService(
        settings=SimpleNamespace(poll_interval_seconds=123),
        session_factory=lambda: _DummySessionContext(SimpleNamespace()),
    )
    runner = _Runner()
    watcher.run_tick = AsyncMock(return_value={"processed_count": 0, "error_count": 0, "latency_ms": 1})  # type: ignore[method-assign]
    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)
        raise asyncio.CancelledError

    import job_agent_runtime.worker.watcher as watcher_module

    monkeypatch.setattr(watcher_module.asyncio, "sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await watcher.watch_loop(pipeline_runner=runner)

    assert delays == [123]

