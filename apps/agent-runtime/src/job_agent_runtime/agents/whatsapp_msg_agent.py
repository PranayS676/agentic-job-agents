from __future__ import annotations

from typing import Any
from uuid import UUID

from job_integrations.waha import WAHAConnector
from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import OutboundResult


class WhatsAppMsgAgent(BaseAgent):
    def __init__(
        self,
        *,
        db_session,
        tracer: AgentTracer,
        settings: Settings | None = None,
        connector: WAHAConnector | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.connector = connector
        super().__init__(
            skill_path="skills/whatsapp-composer",
            model=self.settings.whatsapp_msg_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(self, context: dict[str, Any], trace_id: UUID) -> OutboundResult:
        poster_number = str(context.get("poster_number") or "").strip()
        if not poster_number:
            raise ValueError("poster_number is required for WhatsApp routing")

        attachment_path = str(context.get("attachment_path") or "").strip() or None
        if not attachment_path:
            raise ValueError("attachment_path is required for WhatsApp routing")

        prompt = (
            "Generate a WhatsApp outreach message JSON.\n"
            "Return strict JSON with key: message_text.\n\n"
            f"job_title: {context.get('job_title')}\n"
            f"company: {context.get('company')}\n"
            f"job_summary:\n{context.get('job_summary')}\n"
            f"recipient_number: {poster_number}\n"
            f"attachment_path: {attachment_path}\n"
        )
        model_result = await self._call_model(
            messages=[{"role": "user", "content": prompt}],
            trace_id=trace_id,
            max_tokens=768,
        )
        parsed = self._parse_json(model_result["text"])
        message_text = str(
            parsed.get("message_text")
            or parsed.get("body")
            or parsed.get("text")
            or ""
        ).strip()
        if not message_text:
            raise ValueError("WhatsApp model output missing message_text")

        connector = self.connector or WAHAConnector(
            base_url=self.settings.waha_base_url,
            session=self.settings.waha_session,
            api_key=self.settings.waha_api_key,
        )
        owns_connector = self.connector is None
        try:
            response = await connector.send_message_with_file(
                to_number=poster_number,
                text=message_text,
                file_path=attachment_path,
            )
        finally:
            if owns_connector:
                await connector.close()

        if not response.get("ok"):
            raise RuntimeError(f"WAHA send_message_with_file failed: {response.get('error')}")

        payload = response.get("data") or {}
        external_id = self._extract_external_id(payload)

        return {
            "sent": True,
            "channel": "whatsapp",
            "recipient": poster_number,
            "subject": None,
            "body_preview": message_text[:500],
            "attachment_path": attachment_path,
            "external_id": external_id,
        }

    def _extract_external_id(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("id", "message_id", "messageId", "msgId"):
                value = payload.get(key)
                if value:
                    return str(value)
            result = payload.get("result")
            if isinstance(result, dict):
                return self._extract_external_id(result)
            if isinstance(result, str) and result.strip():
                return result.strip()
        return None

