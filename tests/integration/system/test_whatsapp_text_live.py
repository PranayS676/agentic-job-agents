from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from unittest.mock import AsyncMock

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
@pytest.mark.live_anthropic
async def test_whatsapp_text_live_generate_via_openrouter_and_send_to_self(tmp_path: Path) -> None:
    if os.getenv("RUN_LIVE_WAHA_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_WAHA_TESTS=1 to enable live WAHA tests.")
    if os.getenv("RUN_LIVE_ANTHROPIC_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_ANTHROPIC_TESTS=1 to enable live Anthropic tests.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live settings are incomplete: {exc}")

    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        pytest.skip("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be configured.")

    connector = WAHAConnector(
        base_url=settings.waha_base_url,
        session=settings.waha_session,
        api_key=settings.waha_api_key,
    )
    try:
        session_response = await connector.client.get(f"/api/sessions/{connector.session}")
        if session_response.status_code != 200:
            pytest.skip("WAHA session info is not reachable; ensure WAHA is running and authenticated.")

        self_number = _extract_self_number(session_response.json())
        if not self_number:
            pytest.skip("Unable to resolve self number from WAHA session payload.")

        # Draft mode uses the real LLM to compose the message but avoids file sending.
        attachment_path = tmp_path / "draft_only_placeholder.docx"
        attachment_path.write_text("placeholder", encoding="utf-8")

        agent = WhatsAppMsgAgent(
            db_session=SimpleNamespace(),
            tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
            settings=settings,
            connector=connector,
        )

        draft_result = await agent.run(
            context={
                "job_title": "Senior Python AI Engineer",
                "company": "Acme",
                "job_summary": (
                    "Contract role building Python, FastAPI, LLM, and AWS systems. "
                    "Reach out with a concise recruiter-friendly message."
                ),
                "poster_number": self_number,
                "attachment_path": str(attachment_path),
                "relevance_decision": "fit",
            },
            trace_id=uuid4(),
            delivery_mode="draft",
        )

        message_text = str(draft_result["body_preview"]).strip()
        assert message_text

        send_result = await connector.send_message(self_number, message_text)
        assert send_result["ok"] is True
        external_id = (send_result.get("data") or {}).get("id") or (send_result.get("data") or {}).get("message_id")
        assert external_id
    finally:
        await connector.close()
        clear_settings_cache()
