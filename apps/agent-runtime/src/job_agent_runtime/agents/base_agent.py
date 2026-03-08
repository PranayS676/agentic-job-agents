from __future__ import annotations

import asyncio
import json
import time
import abc
import pathlib
import typing
from uuid import UUID

from anthropic import AsyncAnthropic

from job_platform.config import Settings, get_settings


class BaseAgent(abc.ABC):
    def __init__(self, skill_path: str, model: str, db_session, tracer) -> None:
        self.model = model
        self.db_session = db_session
        self.tracer = tracer

        self.settings: Settings = get_settings()
        self.client = AsyncAnthropic(api_key=self.settings.anthropic_api_key.get_secret_value())

        self.skill_path = self._resolve_skill_path(skill_path)
        self.system_prompt, self.reference_context = self._load_skill_context()

    def _resolve_skill_path(self, skill_path: str) -> pathlib.Path:
        candidate = pathlib.Path(skill_path)
        if candidate.is_absolute():
            return candidate

        direct_candidate = self.settings.resolve_path(candidate)
        if direct_candidate.exists():
            return direct_candidate

        relative_parts = candidate.parts
        if relative_parts and relative_parts[0] == "skills":
            candidate = pathlib.Path(*relative_parts[1:])

        return self.settings.resolve_path(self.settings.skills_dir / candidate)

    def _load_skill_context(self) -> tuple[str, str]:
        skill_file = self.skill_path / "SKILL.md"
        if not skill_file.is_file():
            raise FileNotFoundError(f"Missing SKILL.md at {skill_file}")

        system_prompt = skill_file.read_text(encoding="utf-8").strip()
        references_dir = self.skill_path / "references"
        reference_context = ""

        if references_dir.is_dir():
            reference_files = sorted(
                [path for path in references_dir.rglob("*") if path.is_file()],
                key=lambda path: path.relative_to(references_dir).as_posix(),
            )
            chunks: list[str] = []
            for path in reference_files:
                relative_name = path.relative_to(references_dir).as_posix()
                content = path.read_text(encoding="utf-8").strip()
                chunks.append(f"[{relative_name}]\n{content}")
            reference_context = "\n\n".join(chunks).strip()

        if reference_context:
            system_prompt = (
                f"{system_prompt}\n\n"
                "=== REFERENCE CONTEXT ===\n"
                f"{reference_context}"
            )

        return system_prompt, reference_context

    @abc.abstractmethod
    async def run(self, input_data: dict, trace_id: UUID) -> dict:
        """Execute the agent workflow."""

    def _parse_json(self, response_text: str) -> dict:
        text = response_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Failed to parse JSON response: {response_text}") from exc

        if not isinstance(parsed, dict):
            raise ValueError(f"Parsed JSON must be an object: {response_text}")
        return parsed

    async def _call_model(
        self,
        messages: list[dict[str, typing.Any]],
        trace_id: UUID,
        tools: list[dict[str, typing.Any]] | None = None,
        max_tokens: int = 2048,
    ) -> dict[str, typing.Any]:
        max_attempts = 3
        backoff_seconds = [0.5, 1.0]
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            started = time.perf_counter()
            try:
                payload: dict[str, typing.Any] = {
                    "model": self.model,
                    "system": self.system_prompt,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                if tools is not None:
                    payload["tools"] = tools

                response = await self.client.messages.create(**payload)
                latency_ms = int((time.perf_counter() - started) * 1000)

                text_output = self._extract_text(response)
                usage = getattr(response, "usage", None)
                input_tokens = getattr(usage, "input_tokens", None)
                output_tokens = getattr(usage, "output_tokens", None)

                raw_response = self._serialize_response(response)
                result = {
                    "text": text_output,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "latency_ms": latency_ms,
                    "model": self.model,
                    "raw_response": raw_response,
                }

                decision_summary = text_output[:200] if text_output else "Model call completed"
                await self.tracer.trace(
                    trace_id=trace_id,
                    agent_name=self.__class__.__name__,
                    model=self.model,
                    input_data={"messages": messages, "tools": tools, "max_tokens": max_tokens},
                    output_data=result,
                    tokens_in=input_tokens,
                    tokens_out=output_tokens,
                    latency_ms=latency_ms,
                    decision_summary=decision_summary,
                )
                return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < max_attempts and self._is_retryable_error(exc):
                    await asyncio.sleep(backoff_seconds[attempt - 1])
                    continue
                break

        assert last_exc is not None
        raise RuntimeError(
            "Model call failed "
            f"(agent={self.__class__.__name__}, model={self.model}, attempts={max_attempts}): "
            f"{last_exc.__class__.__name__}: {last_exc}"
        ) from last_exc

    def _is_retryable_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        if isinstance(status_code, int) and 500 <= status_code < 600:
            return True
        retryable_names = {
            "APIConnectionError",
            "APITimeoutError",
            "RateLimitError",
            "InternalServerError",
        }
        return exc.__class__.__name__ in retryable_names

    def _extract_text(self, response: typing.Any) -> str:
        content = getattr(response, "content", None) or []
        pieces: list[str] = []
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if block_type != "text":
                continue
            text_value = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            if text_value:
                pieces.append(text_value)
        return "\n".join(pieces).strip()

    def _serialize_response(self, response: typing.Any) -> dict[str, typing.Any]:
        if hasattr(response, "model_dump"):
            return response.model_dump(mode="json")
        if hasattr(response, "to_dict"):
            return response.to_dict()
        return {"repr": repr(response)}
