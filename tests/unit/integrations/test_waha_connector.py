from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

from job_integrations.waha import WAHAConnector


def _build_connector(handler) -> WAHAConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://localhost:3000")
    return WAHAConnector(
        client=client,
        base_url="http://localhost:3000",
        session="default",
        api_key="test-key",
    )


@pytest.mark.asyncio
async def test_get_new_messages_normalizes_filters_and_sorts() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/messages"
        assert request.url.params["chatId"] == "123456@g.us"
        assert request.url.params["session"] == "default"
        assert request.url.params["limit"] == "2"
        payload = {
            "messages": [
                {"id": "m3", "message": {"text": "new-2"}, "sender": "3333", "ts": 300},
                {"id": "m1", "text": "old", "author": "1111", "timestamp": 100},
                {"_id": "m2", "body": "new-1", "from": "2222", "time": 200},
                {"id": "m4", "body": "too-new", "from": "4444", "time": 450},
            ]
        }
        return httpx.Response(200, json=payload)

    connector = _build_connector(handler)
    try:
        messages = await connector.get_new_messages(
            "123456@g.us",
            since_timestamp=150,
            until_timestamp=300,
            limit=2,
        )
        assert messages == [
            {
                "id": "m2",
                "text": "new-1",
                "sender_number": "2222",
                "timestamp": 200,
                "group_id": "123456@g.us",
            },
            {
                "id": "m3",
                "text": "new-2",
                "sender_number": "3333",
                "timestamp": 300,
                "group_id": "123456@g.us",
            },
        ]
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_list_groups_filters_only_group_chat_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/default/groups"
        return httpx.Response(
            200,
            json=[
                {"chatId": "11111@g.us", "name": "Group A"},
                {"id": {"_serialized": "22222@g.us"}, "name": "Group B"},
                {"id": {"user": "33333", "server": "g.us"}, "name": "Group C"},
                {"chatId": "12345@c.us", "name": "Direct"},
            ],
        )

    connector = _build_connector(handler)
    try:
        groups = await connector.list_groups()
        assert groups == [
            {"chatId": "11111@g.us", "name": "Group A"},
            {"chatId": "22222@g.us", "name": "Group B"},
            {"chatId": "33333@g.us", "name": "Group C"},
        ]
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_list_groups_falls_back_to_legacy_chats_endpoint() -> None:
    calls = {"count": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if request.url.path == "/api/default/groups":
            return httpx.Response(404, json={"error": "not found"})
        assert request.url.path == "/api/chats"
        return httpx.Response(
            200,
            json=[
                {"chatId": "11111@g.us", "name": "Group A"},
                {"chatId": "12345@c.us", "name": "Direct"},
            ],
        )

    connector = _build_connector(handler)
    try:
        groups = await connector.list_groups()
        assert groups == [{"chatId": "11111@g.us", "name": "Group A"}]
        assert calls["count"] >= 2
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_send_message_success_path() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sendText"
        payload = await request.aread()
        assert b'"chatId":"15550001111@c.us"' in payload
        assert b'"session":"default"' in payload
        return httpx.Response(200, json={"id": "sent-1"})

    connector = _build_connector(handler)
    try:
        result = await connector.send_message("15550001111", "Hello from tests")
        assert result["ok"] is True
        assert result["data"]["id"] == "sent-1"
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_send_message_with_file_success_path(tmp_path: Path) -> None:
    attachment = tmp_path / "resume.pdf"
    attachment.write_bytes(b"%PDF-1.4\nfake\n")

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sendFile"
        assert request.headers["content-type"].startswith("application/json")
        body = json.loads((await request.aread()).decode("utf-8"))
        assert body["chatId"] == "15550001111@c.us"
        assert body["session"] == "default"
        assert body["caption"] == "Please review attached resume"
        assert body["file"]["filename"] == "resume.pdf"
        assert body["file"]["mimetype"] == "application/pdf"
        assert base64.b64decode(body["file"]["data"]) == attachment.read_bytes()
        return httpx.Response(200, json={"id": "file-1"})

    connector = _build_connector(handler)
    try:
        result = await connector.send_message_with_file(
            to_number="15550001111",
            text="Please review attached resume",
            file_path=str(attachment),
        )
        assert result["ok"] is True
        assert result["data"]["id"] == "file-1"
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_send_message_with_file_includes_http_error_body(tmp_path: Path) -> None:
    attachment = tmp_path / "resume.docx"
    attachment.write_text("resume", encoding="utf-8")

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "message": "The feature is available only in Plus version for 'WEBJS' engine.",
                "statusCode": 422,
            },
        )

    connector = _build_connector(handler)
    try:
        result = await connector.send_message_with_file(
            to_number="15550001111",
            text="Please review attached resume",
            file_path=str(attachment),
        )
        assert result["ok"] is False
        assert "Plus version" in str(result["error"])
        assert "422" in str(result["error"])
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_connector_error_handling_does_not_raise() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    connector = _build_connector(handler)
    try:
        messages = await connector.get_new_messages("123456@g.us", since_timestamp=0)
        assert messages == []

        groups = await connector.list_groups()
        assert groups == []

        send_result = await connector.send_message("15550001111", "hi")
        assert send_result["ok"] is False
        assert "unreachable" in str(send_result["error"])
    finally:
        await connector.close()


@pytest.mark.asyncio
async def test_send_message_with_file_returns_error_when_file_missing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "unused"})

    connector = _build_connector(handler)
    try:
        result = await connector.send_message_with_file(
            to_number="15550001111",
            text="missing file",
            file_path="C:/tmp/does-not-exist.pdf",
        )
        assert result["ok"] is False
        assert "file not found" in str(result["error"])
    finally:
        await connector.close()


def test_extract_email_positive_and_negative_cases() -> None:
    assert WAHAConnector.extract_email("Contact me: test.user+jobs@example.co.uk") == "test.user+jobs@example.co.uk"
    assert WAHAConnector.extract_email("No email in this text.") is None


