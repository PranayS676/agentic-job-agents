from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory
from job_agent_runtime.orchestration.manager import ManagerAgent
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[3]
DATASET_PATH = ROOT_DIR / "output" / "relevance_review" / "waha_last_24h_relevance_seed_dataset.json"


class _NullTracer:
    async def trace(self, **kwargs) -> None:  # noqa: ANN003, ARG002
        return None

    async def update_pipeline_status(self, trace_id, status: str, stage_data: dict) -> None:  # noqa: ANN001, ARG002
        return None


def _load_smoke_cases() -> list[dict]:
    if not DATASET_PATH.is_file():
        return []
    payload = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    smoke_cases: list[dict] = []
    for section_keys in (("fit_examples", "relevant_examples"), ("reject_examples",), ("okayish_examples", "borderline_examples")):
        section_cases: list[dict] = []
        for section in section_keys:
            section_cases.extend(payload.get(section, []))
        if section_cases:
            case = dict(section_cases[0])
            case["dataset_section"] = section_keys[0]
            smoke_cases.append(case)
    return smoke_cases


@pytest.mark.asyncio
@pytest.mark.live_anthropic
async def test_manager_relevance_live_smoke_cases() -> None:
    if os.getenv("RUN_LIVE_ANTHROPIC_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_ANTHROPIC_TESTS=1 to enable live Anthropic tests.")
    if not DATASET_PATH.is_file():
        pytest.skip("Local relevance seed dataset is missing under output/relevance_review/.")

    clear_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live Anthropic settings are incomplete: {exc}")

    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        pytest.skip("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be configured.")
    if not settings.manager_model:
        pytest.skip("MANAGER_MODEL is not configured.")

    cases = _load_smoke_cases()
    if not cases:
        pytest.skip("No smoke cases available in the local seed dataset.")

    manager = ManagerAgent(
        db_session=SimpleNamespace(),
        tracer=_NullTracer(),
        settings=settings,
        agent_factory=DefaultStubAgentFactory(settings=settings),
    )

    for case in cases:
        decision = await manager._evaluate_relevance(
            message=SimpleNamespace(
                group_id=case["group_id"],
                sender_number="+15550000001",
                message_text=case["body"],
            ),
            trace_id=uuid4(),
        )

        assert isinstance(decision["relevant"], bool)
        assert 0 <= decision["score"] <= 10
        assert decision["job_title"]
        assert decision["job_summary"]
        assert decision["poster_number"]
