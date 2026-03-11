from __future__ import annotations

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

import httpx
import structlog

from job_platform.config import get_settings


class WAHAConnector:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str | None = None,
        session: str | None = None,
        api_key: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        settings = get_settings() if base_url is None or session is None or api_key is None else None

        self.base_url = base_url or settings.waha_base_url
        self.session = session or settings.waha_session
        self.api_key = api_key or settings.waha_api_key
        self.last_error: str | None = None
        self.logger = structlog.get_logger(__name__).bind(component="waha_connector")

        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
            headers["Authorization"] = f"Bearer {self.api_key}"

        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers=headers,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _request_json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self.client.request(method, path, **kwargs)
            response.raise_for_status()
            data = response.json() if response.content else {}
            self.last_error = None
            return {"ok": True, "data": data}
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text.strip() if exc.response is not None else ""
            error = str(exc)
            if detail:
                error = f"{error} body={detail[:1000]}"
            self.last_error = error
            self.logger.error(
                "waha_request_failed",
                method=method,
                path=path,
                error=error,
            )
            return {"ok": False, "error": error}
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            self.logger.error(
                "waha_request_failed",
                method=method,
                path=path,
                error=str(exc),
            )
            return {"ok": False, "error": str(exc)}

    async def get_new_messages(
        self,
        group_id: str,
        since_timestamp: int,
        *,
        until_timestamp: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        params = {
            "chatId": group_id,
            "limit": limit,
            "session": self.session,
        }
        response = await self._request_json("GET", "/api/messages", params=params)
        if not response.get("ok"):
            return []

        records = self._extract_records(response.get("data"))
        normalized: list[dict[str, Any]] = []
        for record in records:
            timestamp = self._to_int(
                record.get("timestamp")
                or record.get("time")
                or record.get("ts")
                or record.get("date")
            )
            if timestamp <= since_timestamp:
                continue
            if until_timestamp is not None and timestamp > until_timestamp:
                continue

            text_value = record.get("text") or record.get("body") or record.get("message") or ""
            if isinstance(text_value, dict):
                text_value = text_value.get("text") or text_value.get("body") or ""

            sender_number = (
                record.get("sender_number")
                or record.get("author")
                or record.get("from")
                or record.get("sender")
                or ""
            )
            normalized.append(
                {
                    "id": str(record.get("id") or record.get("_id") or record.get("message_id") or ""),
                    "text": str(text_value or ""),
                    "sender_number": str(sender_number or ""),
                    "timestamp": timestamp,
                    "group_id": group_id,
                }
            )
        normalized.sort(key=lambda item: (self._to_int(item.get("timestamp")), str(item.get("id") or "")))
        return normalized

    async def send_message(self, to_number: str, text: str) -> dict[str, Any]:
        payload = {
            "chatId": self._normalize_chat_id(to_number),
            "text": text,
            "session": self.session,
        }
        response = await self._request_json("POST", "/api/sendText", json=payload)
        if response.get("ok"):
            data = response.get("data")
            return {"ok": True, "data": data if isinstance(data, dict) else {"result": data}}
        return {"ok": False, "error": response.get("error")}

    async def send_message_with_file(self, to_number: str, text: str, file_path: str) -> dict[str, Any]:
        path = Path(file_path)
        if not path.is_file():
            return {"ok": False, "error": f"file not found: {file_path}"}

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        payload = {
            "chatId": self._normalize_chat_id(to_number),
            "session": self.session,
            "caption": text,
            "file": {
                "mimetype": content_type,
                "filename": path.name,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            },
        }
        response = await self._request_json("POST", "/api/sendFile", json=payload)

        if response.get("ok"):
            payload = response.get("data")
            return {"ok": True, "data": payload if isinstance(payload, dict) else {"result": payload}}
        return {"ok": False, "error": response.get("error")}

    async def list_groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        seen: set[str] = set()
        requests: list[tuple[str, dict[str, Any]]] = [
            (f"/api/{self.session}/groups", {"params": {"limit": 500}}),
            ("/api/chats", {"params": {"session": self.session}}),
        ]

        for path, kwargs in requests:
            response = await self._request_json("GET", path, **kwargs)
            if not response.get("ok"):
                continue

            records = self._extract_records(response.get("data"))
            for record in records:
                chat_id = self._extract_chat_id(record)
                if not chat_id or not chat_id.endswith("@g.us"):
                    continue
                if chat_id in seen:
                    continue
                seen.add(chat_id)
                groups.append({"chatId": chat_id, "name": record.get("name")})

            if groups:
                return groups
        return groups

    @staticmethod
    def extract_email(text: str) -> str | None:
        match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
        return match.group(0) if match else None

    def _extract_records(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("messages", "results", "data", "chats"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    return [item for item in candidate if isinstance(item, dict)]
        return []

    def _normalize_chat_id(self, value: str) -> str:
        if value.endswith("@g.us") or value.endswith("@c.us"):
            return value
        return f"{value}@c.us"

    def _extract_chat_id(self, record: dict[str, Any]) -> str:
        raw = record.get("chatId") or record.get("id") or record.get("jid") or ""
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            serialized = raw.get("_serialized")
            if isinstance(serialized, str) and serialized:
                return serialized
            user = raw.get("user")
            server = raw.get("server")
            if isinstance(user, str) and isinstance(server, str) and user and server:
                return f"{user}@{server}"
        return ""

    def _to_int(self, value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

