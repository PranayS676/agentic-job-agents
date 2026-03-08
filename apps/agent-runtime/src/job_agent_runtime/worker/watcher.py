from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

import structlog
from sqlalchemy import select, text

from job_platform.config import Settings, get_settings
from job_platform.database import AsyncSessionLocal
from job_platform.models import WhatsAppMessage


class PipelineRunnerProtocol(Protocol):
    async def run(self, message: WhatsAppMessage, trace_id: UUID) -> dict:
        ...


class WatcherService:
    def __init__(self, *, settings: Settings | None = None, session_factory=AsyncSessionLocal) -> None:
        self.settings = settings or get_settings()
        self.session_factory = session_factory
        self.logger = structlog.get_logger(__name__).bind(component="watcher_service")

    async def run_tick(self, pipeline_runner: PipelineRunnerProtocol) -> dict[str, int]:
        started = time.perf_counter()
        processed_count = 0
        error_count = 0

        async with self.session_factory() as session:
            messages = await self._get_pending_messages(session=session, limit=10)

            for message in messages:
                claimed = await self._claim_message(session=session, message_id=message.id)
                if not claimed:
                    continue
                await session.commit()

                trace_id = await self._create_pipeline_run(session=session, message_id=message.id)
                await session.commit()

                try:
                    await pipeline_runner.run(message=message, trace_id=trace_id)
                    await self._mark_message_success(session=session, message_id=message.id)
                    await session.commit()
                    processed_count += 1
                except Exception as exc:  # noqa: BLE001
                    await self._mark_message_failure(
                        session=session,
                        message_id=message.id,
                        error_message=f"{exc.__class__.__name__}: {exc}",
                    )
                    await session.commit()
                    error_count += 1
                    self.logger.error(
                        "watcher_message_failed",
                        message_id=str(message.id),
                        trace_id=str(trace_id),
                        error=str(exc),
                    )

        latency_ms = int((time.perf_counter() - started) * 1000)
        summary = {
            "processed_count": processed_count,
            "error_count": error_count,
            "latency_ms": latency_ms,
        }
        self.logger.info("watcher_tick", **summary)
        return summary

    async def watch_loop(self, pipeline_runner: PipelineRunnerProtocol) -> None:
        while True:
            try:
                await self.run_tick(pipeline_runner=pipeline_runner)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("watcher_loop_error", error=str(exc))
            await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_cleanup_once(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=30)
        async with self.session_factory() as session:
            result = await session.execute(
                text(
                    """
                    DELETE FROM whatsapp_messages
                    WHERE processed = true
                      AND created_at < :cutoff
                    """
                ),
                {"cutoff": cutoff},
            )
            await session.commit()
            deleted = int(result.rowcount or 0)

        self.logger.info("watcher_cleanup", deleted_count=deleted, cutoff=cutoff.isoformat())
        return deleted

    async def deduplication_cleanup(self) -> None:
        while True:
            try:
                await self.run_cleanup_once()
            except Exception as exc:  # noqa: BLE001
                self.logger.error("watcher_cleanup_error", error=str(exc))
            await asyncio.sleep(24 * 60 * 60)

    async def run_forever(self, pipeline_runner: PipelineRunnerProtocol) -> None:
        await asyncio.gather(
            self.watch_loop(pipeline_runner=pipeline_runner),
            self.deduplication_cleanup(),
        )

    async def _get_pending_messages(self, session, limit: int) -> list[WhatsAppMessage]:
        result = await session.execute(
            select(WhatsAppMessage)
            .where(WhatsAppMessage.processed.is_(False))
            .order_by(WhatsAppMessage.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def _claim_message(self, session, message_id: UUID) -> bool:
        result = await session.execute(
            text(
                """
                UPDATE whatsapp_messages
                SET processing_started_at = NOW()
                WHERE id = :message_id
                  AND processed = false
                  AND processing_started_at IS NULL
                """
            ),
            {"message_id": message_id},
        )
        return bool(result.rowcount and result.rowcount > 0)

    async def _create_pipeline_run(self, session, message_id: UUID) -> UUID:
        result = await session.execute(
            text(
                """
                INSERT INTO pipeline_runs (message_id, status)
                VALUES (:message_id, 'started')
                RETURNING trace_id
                """
            ),
            {"message_id": message_id},
        )
        return result.scalar_one()

    async def _mark_message_success(self, session, message_id: UUID) -> None:
        await session.execute(
            text(
                """
                UPDATE whatsapp_messages
                SET processed = true,
                    processing_error = NULL
                WHERE id = :message_id
                """
            ),
            {"message_id": message_id},
        )

    async def _mark_message_failure(self, session, message_id: UUID, error_message: str) -> None:
        await session.execute(
            text(
                """
                UPDATE whatsapp_messages
                SET processed = true,
                    processing_error = :processing_error
                WHERE id = :message_id
                """
            ),
            {
                "message_id": message_id,
                "processing_error": error_message[:2000],
            },
        )

