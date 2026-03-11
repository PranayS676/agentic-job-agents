from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from job_integrations.gmail import GmailConnector
from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import OutboundResult


class GmailAgent(BaseAgent):
    def __init__(
        self,
        *,
        db_session,
        tracer: AgentTracer,
        settings: Settings | None = None,
        connector: GmailConnector | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.connector = connector or GmailConnector(settings=self.settings)
        super().__init__(
            skill_path="skills/gmail-composer",
            model=self.settings.gmail_agent_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(
        self,
        context: dict[str, Any],
        trace_id: UUID,
        delivery_mode: Literal["send", "draft"] = "send",
    ) -> OutboundResult:
        poster_email = str(context.get("poster_email") or "").strip()
        if not poster_email:
            raise ValueError("poster_email is required for Gmail routing")

        attachment_path = str(context.get("attachment_path") or "").strip() or None
        if not attachment_path:
            raise ValueError("attachment_path is required for Gmail routing")

        prompt = (
            "Generate outreach email JSON for a job application.\n"
            "Return strict JSON with keys: subject, body.\n\n"
            f"job_title: {context.get('job_title')}\n"
            f"company: {context.get('company')}\n"
            f"job_summary:\n{context.get('job_summary')}\n"
            f"relevance_decision: {context.get('relevance_decision')}\n"
            f"delivery_mode: {delivery_mode}\n"
            f"work_type_hints: {self._derive_work_type_hints(str(context.get('job_summary') or ''))}\n"
            f"recipient_email: {poster_email}\n"
            f"attachment_path: {attachment_path}\n"
        )
        model_result = await self._call_model(
            messages=[{"role": "user", "content": prompt}],
            trace_id=trace_id,
            max_tokens=1536,
        )
        parsed = self._parse_json(model_result["text"])

        subject = str(parsed.get("subject") or "").strip()
        body = str(parsed.get("body") or "").strip()
        if not subject:
            raise ValueError("Gmail model output missing subject")
        if not body:
            raise ValueError("Gmail model output missing body")

        message_id: str | None = None
        sent = False
        if delivery_mode == "send":
            message_id = await self.connector.send(
                to=poster_email,
                subject=subject,
                body=body,
                attachment_path=attachment_path,
            )
            sent = True

        return {
            "sent": sent,
            "channel": "email",
            "recipient": poster_email,
            "subject": subject,
            "body_preview": body[:500],
            "attachment_path": attachment_path,
            "external_id": message_id,
        }

    def _derive_work_type_hints(self, job_summary: str) -> dict[str, bool]:
        job_lower = job_summary.lower()
        return {
            "mentions_contract": bool(re.search(r"\b(contract|contractor|contract-to-hire|c2h)\b", job_lower)),
            "mentions_project": bool(re.search(r"\b(project|project-based|project based)\b", job_lower)),
            "mentions_c2c": bool(re.search(r"\bc2c\b", job_lower)),
            "mentions_w2": bool(re.search(r"\bw2\b", job_lower)),
        }
