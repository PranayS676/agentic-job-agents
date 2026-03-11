from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from job_agent_runtime.agents.base_agent import BaseAgent
from job_agent_runtime.agents.whatsapp_msg_agent import WhatsAppMsgAgent
from job_integrations.waha import WAHAConnector
from job_platform.config import clear_settings_cache, get_settings


def _extract_self_number(session_payload: dict) -> str | None:
    me = session_payload.get("me")
    if not isinstance(me, dict):
        return None
    me_id = str(me.get("id") or "").strip()
    if not me_id or "@c.us" not in me_id:
        return None
    return me_id.split("@", 1)[0]


@pytest.mark.asyncio
@pytest.mark.live_waha
async def test_whatsapp_msg_live_send_to_self_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    if os.getenv("RUN_LIVE_WAHA_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_WAHA_TESTS=1 to enable live WAHA tests.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live WAHA settings are incomplete: {exc}")

    connector = WAHAConnector()
    try:
        session_response = await connector.client.get(f"/api/sessions/{connector.session}")
        if session_response.status_code != 200:
            pytest.skip("WAHA session info is not reachable; ensure WAHA is running and authenticated.")

        self_number = _extract_self_number(session_response.json())
        if not self_number:
            pytest.skip("Unable to resolve self number from WAHA session payload.")

        attachment_path = tmp_path / "whatsapp_live_test.docx"
        attachment_path.write_text("live whatsapp docx test", encoding="utf-8")

        async def _fake_call_model(self, messages, trace_id, tools=None, max_tokens=2048):  # noqa: ANN001, ARG002
            _ = (messages, trace_id, tools, max_tokens)
            return {"text": json.dumps({"message_text": "Live test message with attached resume."})}

        monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)

        agent = WhatsAppMsgAgent(
            db_session=SimpleNamespace(),
            tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
            settings=settings,
            connector=connector,
        )

        try:
            result = await agent.run(
                context={
                    "job_title": "ML Engineer",
                    "company": "Acme",
                    "job_summary": "Python and ML contract role",
                    "poster_number": self_number,
                    "attachment_path": str(attachment_path),
                    "relevance_decision": "fit",
                },
                trace_id=uuid4(),
                delivery_mode="send",
            )
        except RuntimeError as exc:
            if "Plus version" in str(exc):
                pytest.skip("Current WAHA tier/engine does not support sendFile for attachments.")
            raise

        assert result["sent"] is True
        assert result["channel"] == "whatsapp"
        assert result["recipient"] == self_number
    finally:
        await connector.close()
        clear_settings_cache()


@pytest.mark.asyncio
@pytest.mark.live_waha
async def test_whatsapp_msg_live_draft_mode_does_not_call_waha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if os.getenv("RUN_LIVE_WAHA_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_WAHA_TESTS=1 to enable live WAHA tests.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live WAHA settings are incomplete: {exc}")

    attachment_path = tmp_path / "whatsapp_live_draft_test.docx"
    attachment_path.write_text("live whatsapp draft docx test", encoding="utf-8")

    async def _fake_call_model(self, messages, trace_id, tools=None, max_tokens=2048):  # noqa: ANN001, ARG002
        _ = (messages, trace_id, tools, max_tokens)
        return {"text": json.dumps({"message_text": "Draft review body."})}

    monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)

    connector = SimpleNamespace(
        send_message_with_file=AsyncMock(return_value={"ok": True, "data": {"id": "should-not-send"}}),
        close=AsyncMock(),
    )
    agent = WhatsAppMsgAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=connector,
    )

    result = await agent.run(
        context={
            "job_title": "Cloud Data Engineer",
            "company": "Acme",
            "job_summary": "Project-based cloud data role on W2",
            "poster_number": "+15550001111",
            "attachment_path": str(attachment_path),
            "relevance_decision": "okayish",
        },
        trace_id=uuid4(),
        delivery_mode="draft",
    )

    assert result["sent"] is False
    assert result["channel"] == "whatsapp"
    assert result["external_id"] is None
    connector.send_message_with_file.assert_not_awaited()
