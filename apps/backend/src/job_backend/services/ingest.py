from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from job_platform.config import get_settings

from job_backend.polling.waha_polling import WahaPollingService


class IngestService:
    def __init__(
        self,
        *,
        settings=None,
        connector=None,
        polling_service: WahaPollingService | None = None,
        enable_polling: bool = True,
    ) -> None:
        self.settings = settings or get_settings()
        self.enable_polling = enable_polling

        self.logger = structlog.get_logger(__name__).bind(component="ingest_service")
        self.last_webhook_at: datetime | None = None
        self._polling_task: asyncio.Task | None = None
        if polling_service is not None:
            self.polling_service = polling_service
        else:
            self.polling_service = WahaPollingService(
                ingest_message=self.ingest_normalized_message,
                settings=self.settings,
                connector=connector,
            )

    @property
    def last_poll_at(self) -> datetime | None:
        return self.polling_service.last_poll_at

    async def start(self) -> None:
        if not self.settings.whatsapp_group_ids_list:
            raise RuntimeError("WHATSAPP_GROUP_IDS must not be empty")
        if self.enable_polling:
            self._polling_task = asyncio.create_task(
                self.polling_service.run_forever(),
                name="ingest_poll_loop",
            )

    async def stop(self) -> None:
        if self._polling_task:
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
        await self.polling_service.close()

    async def ingest_payload(self, payload: dict[str, Any], source: str) -> str:
        normalized = self.normalize_payload(payload)
        if not normalized:
            return "invalid_payload"
        return await self.ingest_normalized_message(normalized, source=source)

    async def ingest_normalized_message(self, normalized: dict[str, Any], source: str) -> str:
        group_id = str(normalized.get("group_id") or "").strip()
        sender_number = str(normalized.get("sender_number") or "").strip()
        message_text = str(normalized.get("message_text") or normalized.get("text") or "").strip()
        source_timestamp = self._to_int(normalized.get("timestamp"))
        if source_timestamp <= 0:
            source_timestamp = int(datetime.now(UTC).timestamp())
        external_message_id = str(
            normalized.get("external_message_id") or normalized.get("id") or ""
        ).strip() or None

        if not group_id or not sender_number or not message_text:
            return "invalid_payload"

        if group_id not in self.settings.whatsapp_group_ids_list:
            self.logger.info("ingest_group_rejected", source=source, group_id=group_id)
            return "ignored_group"

        message_hash = self._message_hash(
            group_id=group_id,
            sender_number=sender_number,
            message_text=message_text,
            source_timestamp=source_timestamp,
            external_message_id=external_message_id,
        )

        # Lazy imports to avoid config hard-fail during module import in tests.
        from job_platform.database import AsyncSessionLocal
        from job_platform.models import WhatsAppMessage

        async with AsyncSessionLocal() as session:
            try:
                duplicate_lookup = select(WhatsAppMessage.id)
                if external_message_id:
                    duplicate_lookup = duplicate_lookup.where(WhatsAppMessage.group_id == group_id)
                    duplicate_lookup = duplicate_lookup.where(
                        WhatsAppMessage.external_message_id == external_message_id
                    )
                else:
                    duplicate_lookup = duplicate_lookup.where(WhatsAppMessage.message_hash == message_hash)

                exists = await session.scalar(duplicate_lookup)
                if exists:
                    self.logger.info(
                        "ingest_duplicate_skipped",
                        source=source,
                        group_id=group_id,
                        message_hash=message_hash,
                    )
                    return "duplicate_ignored"

                session.add(
                    WhatsAppMessage(
                        group_id=group_id,
                        sender_number=sender_number,
                        message_text=message_text,
                        source_timestamp=source_timestamp,
                        external_message_id=external_message_id,
                        ingest_source=source,
                        message_hash=message_hash,
                        processed=False,
                    )
                )
                await session.commit()
                self.logger.info(
                    "ingest_webhook_processed",
                    source=source,
                    group_id=group_id,
                    sender_number=sender_number,
                )
                return "inserted"
            except IntegrityError:
                await session.rollback()
                self.logger.info(
                    "ingest_duplicate_skipped",
                    source=source,
                    group_id=group_id,
                    message_hash=message_hash,
                )
                return "duplicate_ignored"
            except SQLAlchemyError:
                await session.rollback()
                raise

    def normalize_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None

        candidates = [payload]
        for key in ("payload", "message", "data", "body"):
            value = payload.get(key)
            if isinstance(value, dict):
                candidates.append(value)

        def pick(*keys: str) -> Any:
            for candidate in candidates:
                for key in keys:
                    if key in candidate and candidate[key] is not None:
                        return candidate[key]
            return None

        group_id = pick("group_id", "chatId", "chat_id")
        raw_from = pick("from", "fromNumber", "from_number")
        if not group_id and isinstance(raw_from, str) and raw_from.endswith("@g.us"):
            group_id = raw_from

        sender_number = pick("sender_number", "sender", "author", "fromNumber", "from_number")
        if not sender_number and isinstance(raw_from, str) and not raw_from.endswith("@g.us"):
            sender_number = raw_from

        text_value = pick("message_text", "text", "body", "message", "content")
        if isinstance(text_value, dict):
            text_value = text_value.get("text") or text_value.get("body") or text_value.get("content")

        timestamp = self._to_int(pick("timestamp", "time", "ts", "date"))
        if timestamp <= 0:
            timestamp = int(datetime.now(UTC).timestamp())

        external_message_id = pick("external_message_id", "message_id", "id")
        if isinstance(external_message_id, dict):
            external_message_id = external_message_id.get("id")

        normalized = {
            "group_id": str(group_id or "").strip(),
            "sender_number": str(sender_number or "").strip(),
            "message_text": str(text_value or "").strip(),
            "timestamp": timestamp,
            "external_message_id": str(external_message_id or "").strip() or None,
        }
        if not normalized["group_id"] or not normalized["sender_number"] or not normalized["message_text"]:
            return None
        return normalized

    def _message_hash(
        self,
        *,
        group_id: str,
        sender_number: str,
        message_text: str,
        source_timestamp: int,
        external_message_id: str | None,
    ) -> str:
        payload = (
            f"{group_id}|{sender_number}|{message_text}|{source_timestamp}|{external_message_id or ''}"
        ).encode("utf-8")
        return hashlib.md5(payload).hexdigest()

    def _to_int(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0


def create_app(*, enable_polling: bool = True, service: IngestService | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if service is None:
            app.state.ingest_service = IngestService(enable_polling=enable_polling)
        else:
            app.state.ingest_service = service
        await app.state.ingest_service.start()
        try:
            yield
        finally:
            await app.state.ingest_service.stop()

    app = FastAPI(title="WhatsApp Ingest Service", lifespan=lifespan)

    @app.post("/webhook/waha")
    async def webhook_waha(request: Request):
        ingest_service: IngestService = request.app.state.ingest_service
        ingest_service.last_webhook_at = datetime.now(UTC)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=202, content={"status": "invalid_payload"})

        try:
            status = await ingest_service.ingest_payload(payload, source="webhook")
        except SQLAlchemyError:
            ingest_service.logger.error("ingest_webhook_db_error")
            return JSONResponse(status_code=503, content={"status": "db_unavailable"})
        except Exception as exc:  # noqa: BLE001
            ingest_service.logger.error("ingest_webhook_unexpected_error", error=str(exc))
            return JSONResponse(status_code=500, content={"status": "internal_error"})

        if status == "inserted":
            return JSONResponse(status_code=200, content={"status": "processed"})
        if status == "duplicate_ignored":
            return JSONResponse(status_code=200, content={"status": "duplicate_ignored"})
        if status == "ignored_group":
            return JSONResponse(status_code=202, content={"status": "ignored_group"})
        return JSONResponse(status_code=202, content={"status": "invalid_payload"})

    @app.get("/health")
    async def health(request: Request):
        ingest_service: IngestService = request.app.state.ingest_service
        payload: dict[str, Any] = {
            "status": "ok",
            "groups_monitored": len(ingest_service.settings.whatsapp_group_ids_list),
            "polling_enabled": ingest_service.enable_polling,
            "last_poll_started_at": (
                ingest_service.polling_service.last_poll_started_at.isoformat()
                if ingest_service.polling_service.last_poll_started_at
                else None
            ),
            "last_poll_completed_at": (
                ingest_service.polling_service.last_poll_completed_at.isoformat()
                if ingest_service.polling_service.last_poll_completed_at
                else None
            ),
            "polling_status": ingest_service.polling_service.polling_status,
        }
        if ingest_service.last_poll_at:
            payload["last_poll_at"] = ingest_service.last_poll_at.isoformat()
        if ingest_service.last_webhook_at:
            payload["last_webhook_at"] = ingest_service.last_webhook_at.isoformat()
        return payload

    return app


app = create_app()

