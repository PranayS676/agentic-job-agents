from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import Settings, get_settings
from job_platform.tracer import AgentTracer

from .contracts import ResearchActionItem, ResearchOutput


class ResearchAgent(BaseAgent):
    def __init__(
        self,
        *,
        db_session: AsyncSession,
        tracer: AgentTracer,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        super().__init__(
            skill_path="skills/resume-research",
            model=self.settings.research_model,
            db_session=db_session,
            tracer=tracer,
        )

    async def run(self, job_data: dict, trace_id: UUID) -> ResearchOutput:
        resume_text = self._load_base_resume_text()
        job_summary = str(job_data.get("job_summary") or "").strip()
        if not job_summary:
            raise ValueError("job_data.job_summary is required for research")

        user_message = f"Job Summary:\n{job_summary}\n\nCandidate Resume:\n{resume_text}"
        model_result = await self._call_model(
            messages=[{"role": "user", "content": user_message}],
            trace_id=trace_id,
        )
        parsed = self._parse_json(model_result["text"])
        research_output = self._coerce_research_output(parsed)

        await self._persist_research_output(trace_id=trace_id, research_output=research_output)

        decision_summary = (
            f"Found {len(research_output['add_items'])} additions, "
            f"{len(research_output['remove_items'])} removals. ATS estimate: "
            f"{research_output['ats_score_estimate_before']} -> {research_output['ats_score_estimate_after']}"
        )
        await self.tracer.trace(
            trace_id=trace_id,
            agent_name=self.__class__.__name__,
            model=self.model,
            input_data={"job_data": job_data},
            output_data={"research_output": research_output},
            tokens_in=model_result.get("input_tokens"),
            tokens_out=model_result.get("output_tokens"),
            latency_ms=model_result.get("latency_ms"),
            decision_summary=decision_summary,
        )

        return research_output

    def _load_base_resume_text(self) -> str:
        if self.settings.base_resume_text is None:
            raise FileNotFoundError("BASE_RESUME_TEXT is not configured")
        resume_path = self.settings.resolve_path(self.settings.base_resume_text)
        if not resume_path.is_file():
            raise FileNotFoundError(f"BASE_RESUME_TEXT file not found: {resume_path}")
        return resume_path.read_text(encoding="utf-8")

    def _coerce_research_output(self, payload: dict[str, Any]) -> ResearchOutput:
        required_keys = {
            "add_items",
            "remove_items",
            "keywords_to_inject",
            "sections_to_edit",
            "ats_score_estimate_before",
            "ats_score_estimate_after",
            "research_reasoning",
        }
        missing = sorted(required_keys - set(payload))
        if missing:
            raise ValueError(f"Research output missing required keys: {', '.join(missing)}")

        add_items = self._normalize_action_items(
            payload.get("add_items"),
            field_name="add_items",
            max_items=5,
            require_priority=True,
        )
        remove_items = self._normalize_action_items(
            payload.get("remove_items"),
            field_name="remove_items",
            max_items=3,
            require_priority=False,
        )
        keywords = self._normalize_string_list(payload.get("keywords_to_inject"), "keywords_to_inject")
        sections = self._normalize_string_list(payload.get("sections_to_edit"), "sections_to_edit")
        before_score = self._normalize_score(
            payload.get("ats_score_estimate_before"),
            "ats_score_estimate_before",
        )
        after_score = self._normalize_score(
            payload.get("ats_score_estimate_after"),
            "ats_score_estimate_after",
        )
        reasoning = str(payload.get("research_reasoning") or "").strip()
        if not reasoning:
            raise ValueError("research_reasoning must be a non-empty string")

        return {
            "add_items": add_items,
            "remove_items": remove_items,
            "keywords_to_inject": keywords,
            "sections_to_edit": sections,
            "ats_score_estimate_before": before_score,
            "ats_score_estimate_after": after_score,
            "research_reasoning": reasoning,
        }

    def _normalize_action_items(
        self,
        value: Any,
        *,
        field_name: str,
        max_items: int,
        require_priority: bool,
    ) -> list[ResearchActionItem]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        if len(value) > max_items:
            raise ValueError(f"{field_name} exceeds max {max_items} items")

        normalized: list[ResearchActionItem] = []
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise ValueError(f"{field_name}[{index}] must be an object")

            section = str(raw.get("section") or "").strip()
            action = str(raw.get("action") or "").strip()
            reason = str(raw.get("reason") or "").strip()
            if not section or not action or not reason:
                raise ValueError(f"{field_name}[{index}] must include section/action/reason")

            item: ResearchActionItem = {
                "section": section,
                "action": action,
                "reason": reason,
            }

            priority_raw = raw.get("priority")
            if require_priority:
                if priority_raw is None:
                    raise ValueError(f"{field_name}[{index}] priority is required")
                priority = self._normalize_priority(priority_raw, f"{field_name}[{index}].priority")
                item["priority"] = priority
            elif priority_raw is not None:
                item["priority"] = self._normalize_priority(priority_raw, f"{field_name}[{index}].priority")

            normalized.append(item)

        return normalized

    def _normalize_string_list(self, value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{field_name} must be a list")
        seen: set[str] = set()
        result: list[str] = []
        for index, raw in enumerate(value):
            cleaned = str(raw or "").strip()
            if not cleaned:
                raise ValueError(f"{field_name}[{index}] must be a non-empty string")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            result.append(cleaned)
        return result

    def _normalize_score(self, value: Any, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if parsed < 0 or parsed > 100:
            raise ValueError(f"{field_name} must be between 0 and 100")
        return parsed

    def _normalize_priority(self, value: Any, field_name: str) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be an integer") from exc
        if parsed < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return parsed

    async def _persist_research_output(self, trace_id: UUID, research_output: ResearchOutput) -> None:
        update_stmt = text(
            """
            UPDATE pipeline_runs
            SET research_output = CAST(:research_output AS jsonb)
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            update_stmt,
            {
                "trace_id": trace_id,
                "research_output": json.dumps(research_output),
            },
        )
        if result.rowcount is None or result.rowcount == 0:
            raise ValueError(f"pipeline_runs row not found for trace_id={trace_id}")
        await self.db_session.flush()

