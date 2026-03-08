from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import groupby
from typing import Any, Awaitable, Callable

import structlog

from job_integrations.waha import WAHAConnector
from job_platform.config import Settings, get_settings


FIRST_RUN_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_BATCH_LIMIT = 500
MAX_CATCHUP_BATCHES_PER_GROUP = 200
SUCCESS_STATUSES = {"inserted", "duplicate_ignored"}


class WahaPollingService:
    def __init__(
        self,
        *,
        ingest_message: Callable[[dict[str, Any], str], Awaitable[str]],
        settings: Settings | None = None,
        connector: WAHAConnector | None = None,
        session_factory: Callable[[], Any] | None = None,
        now_provider: Callable[[], datetime] | None = None,
        sleep_func: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.ingest_message = ingest_message
        self.connector = connector or WAHAConnector()
        self.logger = structlog.get_logger(__name__).bind(component="waha_polling_service")

        if session_factory is None:
            from job_platform.database import AsyncSessionLocal

            self.session_factory = AsyncSessionLocal
        else:
            self.session_factory = session_factory

        self._now_provider = now_provider or (lambda: datetime.now(UTC))
        self._sleep = sleep_func or asyncio.sleep
        self.last_poll_started_at: datetime | None = None
        self.last_poll_completed_at: datetime | None = None
        self.polling_status = "idle"

    @property
    def last_poll_at(self) -> datetime | None:
        return self.last_poll_completed_at

    async def close(self) -> None:
        await self.connector.close()

    async def run_once(self, *, cutoff_timestamp: int | None = None) -> dict[str, int]:
        started_at = self._now()
        self.last_poll_started_at = started_at
        self.polling_status = "running"
        cutoff_ts = cutoff_timestamp if cutoff_timestamp is not None else int(started_at.timestamp())
        processed_count = 0
        error_count = 0

        for group_id in self.settings.whatsapp_group_ids_list:
            try:
                summary = await self._poll_group(
                    group_id=group_id,
                    started_at=started_at,
                    cutoff_timestamp=cutoff_ts,
                )
                processed_count += summary["processed_count"]
                error_count += summary["error_count"]
            except Exception as exc:  # noqa: BLE001
                error_count += 1
                self.logger.error(
                    "ingest_poll_group_unexpected_error",
                    group_id=group_id,
                    error=str(exc),
                )

        self.last_poll_completed_at = self._now()
        self.polling_status = "error" if error_count else "ok"

        summary = {
            "processed_count": processed_count,
            "error_count": error_count,
        }
        self.logger.info(
            "ingest_poll_tick",
            groups=len(self.settings.whatsapp_group_ids_list),
            cutoff_timestamp=cutoff_ts,
            polling_status=self.polling_status,
            **summary,
        )
        return summary

    async def run_forever(self) -> None:
        next_poll_due_at: datetime | None = None

        while True:
            try:
                now = self._now()
                if next_poll_due_at is None or now >= next_poll_due_at:
                    await self.run_once()
                    next_poll_due_at = self._now() + timedelta(seconds=self.settings.poll_interval_seconds)
                    continue

                remaining = max((next_poll_due_at - now).total_seconds(), 0.0)
                await self._sleep(min(remaining, 60.0))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.polling_status = "error"
                self.logger.error("ingest_poll_loop_error", error=str(exc))
                next_poll_due_at = self._now() + timedelta(seconds=self.settings.poll_interval_seconds)

    async def _poll_group(
        self,
        *,
        group_id: str,
        started_at: datetime,
        cutoff_timestamp: int,
    ) -> dict[str, int]:
        processed_count = 0
        error_count = 0
        current_cursor = await self._mark_cursor_running(
            group_id=group_id,
            started_at=started_at,
            cutoff_timestamp=cutoff_timestamp,
        )

        for batch_number in range(MAX_CATCHUP_BATCHES_PER_GROUP):
            messages = await self.connector.get_new_messages(
                group_id=group_id,
                since_timestamp=current_cursor,
                until_timestamp=cutoff_timestamp,
                limit=DEFAULT_BATCH_LIMIT,
            )

            if self.connector.last_error:
                error_count += 1
                await self._mark_cursor_error(
                    group_id=group_id,
                    last_successful_timestamp=current_cursor,
                    error=f"WAHA connector error: {self.connector.last_error}",
                    failed_at=self._now(),
                )
                self.logger.error(
                    "ingest_poll_group_error",
                    group_id=group_id,
                    cursor=current_cursor,
                    error=self.connector.last_error,
                )
                return {"processed_count": processed_count, "error_count": error_count}

            if not messages:
                await self._mark_cursor_success(
                    group_id=group_id,
                    last_successful_timestamp=current_cursor,
                    completed_at=self._now(),
                )
                return {"processed_count": processed_count, "error_count": error_count}

            batch_result = await self._process_batch(
                group_id=group_id,
                messages=messages,
                starting_cursor=current_cursor,
                batch_truncated=len(messages) >= DEFAULT_BATCH_LIMIT,
            )
            processed_count += batch_result["processed_count"]

            if batch_result["error"]:
                error_count += 1
                await self._mark_cursor_error(
                    group_id=group_id,
                    last_successful_timestamp=batch_result["last_successful_timestamp"],
                    error=batch_result["error"],
                    failed_at=self._now(),
                )
                return {"processed_count": processed_count, "error_count": error_count}

            if batch_result["last_successful_timestamp"] <= current_cursor:
                error_count += 1
                self.logger.error(
                    "polling_no_progress",
                    group_id=group_id,
                    cursor=current_cursor,
                    batch_number=batch_number + 1,
                    batch_size=len(messages),
                    cutoff_timestamp=cutoff_timestamp,
                )
                await self._mark_cursor_error(
                    group_id=group_id,
                    last_successful_timestamp=current_cursor,
                    error="No cursor progress made while processing the batch",
                    failed_at=self._now(),
                )
                return {"processed_count": processed_count, "error_count": error_count}

            current_cursor = batch_result["last_successful_timestamp"]
            await self._update_cursor_checkpoint(
                group_id=group_id,
                last_successful_timestamp=current_cursor,
                checkpoint_at=self._now(),
            )

        self.logger.warning(
            "polling_catchup_incomplete",
            group_id=group_id,
            cursor=current_cursor,
            cutoff_timestamp=cutoff_timestamp,
            max_batches=MAX_CATCHUP_BATCHES_PER_GROUP,
        )
        await self._mark_cursor_success(
            group_id=group_id,
            last_successful_timestamp=current_cursor,
            completed_at=self._now(),
        )
        return {"processed_count": processed_count, "error_count": error_count}

    async def _process_batch(
        self,
        *,
        group_id: str,
        messages: list[dict[str, Any]],
        starting_cursor: int,
        batch_truncated: bool,
    ) -> dict[str, Any]:
        safe_cursor = starting_cursor
        processed_count = 0
        boundary_timestamp = (
            self._to_int(messages[-1].get("timestamp")) if batch_truncated and messages else None
        )

        for timestamp, group in groupby(messages, key=lambda item: self._to_int(item.get("timestamp"))):
            same_timestamp_messages = list(group)
            for message in same_timestamp_messages:
                try:
                    status = await self.ingest_message(message, "poll")
                except Exception as exc:  # noqa: BLE001
                    self.logger.error(
                        "ingest_poll_message_error",
                        group_id=group_id,
                        message_id=message.get("id"),
                        error=str(exc),
                    )
                    return {
                        "processed_count": processed_count,
                        "last_successful_timestamp": safe_cursor,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }

                if status not in SUCCESS_STATUSES:
                    self.logger.error(
                        "ingest_poll_message_rejected",
                        group_id=group_id,
                        message_id=message.get("id"),
                        status=status,
                    )
                    return {
                        "processed_count": processed_count,
                        "last_successful_timestamp": safe_cursor,
                        "error": f"Ingest returned non-success status: {status}",
                    }
                processed_count += 1

            # If the batch is truncated, avoid advancing into the final timestamp bucket.
            if boundary_timestamp is not None and timestamp == boundary_timestamp:
                break
            safe_cursor = timestamp

        return {
            "processed_count": processed_count,
            "last_successful_timestamp": safe_cursor,
            "error": None,
        }

    async def _mark_cursor_running(
        self,
        *,
        group_id: str,
        started_at: datetime,
        cutoff_timestamp: int,
    ) -> int:
        from job_platform.models import PollingCursor

        async with self.session_factory() as session:
            cursor = await session.get(PollingCursor, group_id)
            if cursor is None:
                cursor = PollingCursor(
                    group_id=group_id,
                    last_successful_message_timestamp=self._bootstrap_since_timestamp(started_at),
                    status="bootstrapped",
                )
                session.add(cursor)

            cursor.last_poll_started_at = started_at
            cursor.last_cutoff_timestamp = cutoff_timestamp
            cursor.status = "running"
            cursor.last_error = None
            cursor.updated_at = started_at
            await session.commit()
            return self._to_int(cursor.last_successful_message_timestamp)

    async def _update_cursor_checkpoint(
        self,
        *,
        group_id: str,
        last_successful_timestamp: int,
        checkpoint_at: datetime,
    ) -> None:
        from job_platform.models import PollingCursor

        async with self.session_factory() as session:
            cursor = await session.get(PollingCursor, group_id)
            if cursor is None:
                raise RuntimeError(f"Polling cursor missing for group {group_id}")

            cursor.last_successful_message_timestamp = last_successful_timestamp
            cursor.status = "running"
            cursor.last_error = None
            cursor.updated_at = checkpoint_at
            await session.commit()

    async def _mark_cursor_success(
        self,
        *,
        group_id: str,
        last_successful_timestamp: int,
        completed_at: datetime,
    ) -> None:
        from job_platform.models import PollingCursor

        async with self.session_factory() as session:
            cursor = await session.get(PollingCursor, group_id)
            if cursor is None:
                raise RuntimeError(f"Polling cursor missing for group {group_id}")

            cursor.last_successful_message_timestamp = last_successful_timestamp
            cursor.last_poll_completed_at = completed_at
            cursor.status = "ok"
            cursor.last_error = None
            cursor.updated_at = completed_at
            await session.commit()

    async def _mark_cursor_error(
        self,
        *,
        group_id: str,
        last_successful_timestamp: int,
        error: str,
        failed_at: datetime,
    ) -> None:
        from job_platform.models import PollingCursor

        async with self.session_factory() as session:
            cursor = await session.get(PollingCursor, group_id)
            if cursor is None:
                raise RuntimeError(f"Polling cursor missing for group {group_id}")

            cursor.last_successful_message_timestamp = last_successful_timestamp
            cursor.last_poll_completed_at = failed_at
            cursor.status = "error"
            cursor.last_error = error
            cursor.updated_at = failed_at
            await session.commit()

    def _bootstrap_since_timestamp(self, reference_time: datetime | None = None) -> int:
        current = reference_time or self._now()
        return int(current.timestamp()) - FIRST_RUN_LOOKBACK_SECONDS

    def _now(self) -> datetime:
        return self._now_provider()

    def _to_int(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
