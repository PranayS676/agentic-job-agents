from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from job_backend.polling.waha_polling import WahaPollingService


@dataclass
class _Settings:
    whatsapp_group_ids_list: list[str]
    poll_interval_seconds: int = 1800


class _Connector:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []
        self.last_error: str | None = None

    async def get_new_messages(
        self,
        group_id: str,
        since_timestamp: int,
        *,
        until_timestamp: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "group_id": group_id,
                "since_timestamp": since_timestamp,
                "until_timestamp": until_timestamp,
                "limit": limit,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return []

    async def close(self) -> None:
        return None


def _build_service(
    *,
    connector: _Connector,
    now_provider=None,
    sleep_func=None,
    ingest_message=None,
) -> WahaPollingService:
    async def _default_ingest(message: dict[str, Any], source: str) -> str:  # noqa: ARG001
        return "inserted"

    return WahaPollingService(
        ingest_message=ingest_message or _default_ingest,
        settings=_Settings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=connector,
        now_provider=now_provider,
        sleep_func=sleep_func,
    )


def test_bootstrap_since_timestamp_uses_last_24_hours() -> None:
    reference_time = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
    service = _build_service(connector=_Connector([]))

    assert service._bootstrap_since_timestamp(reference_time) == int(reference_time.timestamp()) - 86400


@pytest.mark.asyncio
async def test_run_once_processes_messages_and_updates_cursor() -> None:
    connector = _Connector(
        [
            [
                {
                    "id": "m1",
                    "text": "first",
                    "sender_number": "1111",
                    "timestamp": 100,
                    "group_id": "group-1@g.us",
                },
                {
                    "id": "m2",
                    "text": "second",
                    "sender_number": "1111",
                    "timestamp": 200,
                    "group_id": "group-1@g.us",
                },
            ],
            [],
        ]
    )
    service = _build_service(connector=connector, now_provider=lambda: datetime(2026, 3, 7, 12, 0, tzinfo=UTC))

    checkpoints: list[int] = []
    successes: list[int] = []

    async def _mark_cursor_running(**kwargs: Any) -> int:  # noqa: ARG001
        return 0

    async def _update_cursor_checkpoint(**kwargs: Any) -> None:
        checkpoints.append(kwargs["last_successful_timestamp"])

    async def _mark_cursor_success(**kwargs: Any) -> None:
        successes.append(kwargs["last_successful_timestamp"])

    service._mark_cursor_running = _mark_cursor_running  # type: ignore[method-assign]
    service._update_cursor_checkpoint = _update_cursor_checkpoint  # type: ignore[method-assign]
    service._mark_cursor_success = _mark_cursor_success  # type: ignore[method-assign]

    summary = await service.run_once(cutoff_timestamp=300)

    assert summary == {"processed_count": 2, "error_count": 0}
    assert checkpoints == [200]
    assert successes == [200]
    assert connector.calls[0]["until_timestamp"] == 300
    assert connector.calls[0]["limit"] == 500


@pytest.mark.asyncio
async def test_run_once_marks_error_without_advancing_cursor_on_ingest_failure() -> None:
    connector = _Connector(
        [
            [
                {
                    "id": "m1",
                    "text": "boom",
                    "sender_number": "1111",
                    "timestamp": 100,
                    "group_id": "group-1@g.us",
                }
            ]
        ]
    )

    async def _failing_ingest(message: dict[str, Any], source: str) -> str:  # noqa: ARG001
        raise RuntimeError("db down")

    service = _build_service(connector=connector, ingest_message=_failing_ingest)

    async def _mark_cursor_running(**kwargs: Any) -> int:  # noqa: ARG001
        return 42

    errors: list[tuple[int, str]] = []

    async def _mark_cursor_error(**kwargs: Any) -> None:
        errors.append((kwargs["last_successful_timestamp"], kwargs["error"]))

    service._mark_cursor_running = _mark_cursor_running  # type: ignore[method-assign]
    service._mark_cursor_error = _mark_cursor_error  # type: ignore[method-assign]

    summary = await service.run_once(cutoff_timestamp=300)

    assert summary == {"processed_count": 0, "error_count": 1}
    assert errors[0][0] == 42
    assert "RuntimeError: db down" in errors[0][1]


def test_run_forever_runs_immediately_when_overdue_after_sleep() -> None:
    connector = _Connector([])
    current_time = {"value": datetime(2026, 3, 7, 12, 0, tzinfo=UTC)}
    run_calls: list[datetime] = []
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        current_time["value"] = current_time["value"] + timedelta(seconds=1801)

    service = _build_service(
        connector=connector,
        now_provider=lambda: current_time["value"],
        sleep_func=_fake_sleep,
    )

    async def _fake_run_once(*, cutoff_timestamp: int | None = None) -> dict[str, int]:  # noqa: ARG001
        run_calls.append(current_time["value"])
        if len(run_calls) == 2:
            raise asyncio.CancelledError
        return {"processed_count": 0, "error_count": 0}

    service.run_once = _fake_run_once  # type: ignore[method-assign]

    asyncio.run(service.run_forever())

    assert len(run_calls) == 2
    assert run_calls[1] > run_calls[0]
    assert sleep_calls[0] == 60.0
