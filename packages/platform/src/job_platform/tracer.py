from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class AgentTracer:
    def __init__(self, db_session: AsyncSession) -> None:
        self.db_session = db_session

    async def trace(
        self,
        trace_id: UUID | None,
        agent_name: str,
        model: str,
        input_data: dict[str, Any] | None,
        output_data: dict[str, Any] | None,
        tokens_in: int | None,
        tokens_out: int | None,
        latency_ms: int | None,
        decision_summary: str | None,
    ) -> None:
        self._ensure_json_serializable(input_data, "input_data")
        self._ensure_json_serializable(output_data, "output_data")

        stmt = text(
            """
            INSERT INTO agent_traces (
                trace_id,
                agent_name,
                model,
                input_tokens,
                output_tokens,
                latency_ms,
                decision,
                full_input,
                full_output
            ) VALUES (
                :trace_id,
                :agent_name,
                :model,
                :input_tokens,
                :output_tokens,
                :latency_ms,
                :decision,
                CAST(:full_input AS jsonb),
                CAST(:full_output AS jsonb)
            )
            """
        )
        await self.db_session.execute(
            stmt,
            {
                "trace_id": trace_id,
                "agent_name": agent_name,
                "model": model,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "latency_ms": latency_ms,
                "decision": decision_summary,
                "full_input": self._to_json_string(input_data),
                "full_output": self._to_json_string(output_data),
            },
        )
        await self.db_session.flush()

    async def update_pipeline_status(
        self,
        trace_id: UUID,
        status: str,
        stage_data: dict[str, Any],
    ) -> None:
        if not isinstance(stage_data, dict):
            raise ValueError("stage_data must be a dict")
        self._ensure_json_serializable(stage_data, "stage_data")

        event = {
            "status": status,
            "stage_data": stage_data,
            "ts": datetime.now(UTC).isoformat(),
        }
        event_json = json.dumps(event)

        stmt = text(
            """
            UPDATE pipeline_runs
            SET status = :status,
                manager_decision = jsonb_set(
                    COALESCE(manager_decision, '{}'::jsonb),
                    '{events}',
                    COALESCE(COALESCE(manager_decision, '{}'::jsonb)->'events', '[]'::jsonb)
                        || jsonb_build_array(CAST(:event_json AS jsonb)),
                    true
                )
            WHERE trace_id = :trace_id
            """
        )
        result = await self.db_session.execute(
            stmt,
            {
                "status": status,
                "event_json": event_json,
                "trace_id": trace_id,
            },
        )
        if result.rowcount == 0:
            raise ValueError(f"pipeline_runs row not found for trace_id={trace_id}")
        await self.db_session.flush()

    def _ensure_json_serializable(self, value: Any, field_name: str) -> None:
        if value is None:
            return
        try:
            json.dumps(value)
        except TypeError as exc:
            raise ValueError(f"{field_name} must be JSON-serializable") from exc

    def _to_json_string(self, value: Any) -> str | None:
        if value is None:
            return None
        return json.dumps(value)
