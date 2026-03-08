from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from job_agent_runtime.agents.research_agent import ResearchAgent
from job_platform.config import clear_settings_cache


def _set_required_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    create_resume_text: bool = True,
) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = tmp_path / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    if create_resume_text:
        (data_dir / "base_resume.md").write_text("resume text", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")

    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "MANAGER_MODEL": "claude-opus-4-6",
        "RESEARCH_MODEL": "claude-sonnet-4-6",
        "RESUME_EDITOR_MODEL": "claude-sonnet-4-6",
        "PDF_CONVERTER_MODEL": "claude-haiku-4-5-20251001",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/jobagent",
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": "GROUP1@g.us",
        "POLL_INTERVAL_SECONDS": "30",
        "GMAIL_CREDENTIALS_PATH": str(data_dir / "credentials.json"),
        "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        "SENDER_EMAIL": "sender@example.com",
        "SENDER_NAME": "Pranay",
        "MIN_RELEVANCE_SCORE": "6",
        "MIN_ATS_SCORE": "65",
        "MAX_RESUME_EDIT_ITERATIONS": "2",
        "BASE_RESUME_DOCX": str(data_dir / "base_resume.docx"),
        "BASE_RESUME_TEXT": str(data_dir / "base_resume.md"),
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def _create_skill_files(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "resume-research"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# Resume Research Agent", encoding="utf-8")
    (references_dir / "research_methodology.md").write_text(
        "1. Read full JD\n2. Prioritize explicit requirements",
        encoding="utf-8",
    )


def _setup_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, create_resume_text: bool = True):
    _set_required_env(monkeypatch, tmp_path, create_resume_text=create_resume_text)
    _create_skill_files(tmp_path)
    monkeypatch.chdir(tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(
        base_agent_module,
        "AsyncAnthropic",
        lambda api_key: SimpleNamespace(messages=SimpleNamespace(create=AsyncMock())),  # noqa: ARG005
    )

    db_session = SimpleNamespace(execute=AsyncMock(), flush=AsyncMock())
    tracer = SimpleNamespace(trace=AsyncMock())
    agent = ResearchAgent(db_session=db_session, tracer=tracer)
    return agent, db_session, tracer


def _valid_research_payload() -> dict:
    return {
        "add_items": [
            {
                "section": "skills",
                "action": "Add Anthropic SDK experience",
                "reason": "Direct keyword in JD",
                "priority": 1,
            }
        ],
        "remove_items": [
            {
                "section": "experience_old_job",
                "action": "Shorten unrelated Java bullets",
                "reason": "Not relevant for this role",
            }
        ],
        "keywords_to_inject": ["LLM", "RAG", "Anthropic SDK"],
        "sections_to_edit": ["summary", "skills"],
        "ats_score_estimate_before": 42,
        "ats_score_estimate_after": 76,
        "research_reasoning": "Resume needs stronger LLM evidence and keyword alignment.",
    }


@pytest.fixture(autouse=True)
def _clean_settings_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_text", "expected_after"),
    [
        (json.dumps(_valid_research_payload()), 76),
        (
            "```json\n" + json.dumps(_valid_research_payload()) + "\n```",
            76,
        ),
    ],
)
async def test_run_success_persists_output_and_traces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model_text: str,
    expected_after: int,
) -> None:
    agent, db_session, tracer = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    db_session.execute.return_value = SimpleNamespace(rowcount=1)
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": model_text, "input_tokens": 10, "output_tokens": 20, "latency_ms": 3}
    )

    result = await agent.run(job_data={"job_summary": "Python LLM role"}, trace_id=trace_id)

    assert result["ats_score_estimate_after"] == expected_after
    assert len(result["add_items"]) == 1
    assert len(result["remove_items"]) == 1
    assert db_session.execute.await_count == 1
    assert db_session.flush.await_count == 1
    execute_args = db_session.execute.await_args.args
    params = execute_args[1]
    assert params["trace_id"] == trace_id
    persisted = json.loads(params["research_output"])
    assert "research_reasoning" in persisted
    assert tracer.trace.await_count == 1
    assert "Found 1 additions, 1 removals" in tracer.trace.await_args.kwargs["decision_summary"]


@pytest.mark.asyncio
async def test_run_invalid_payload_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps({"add_items": []})}
    )

    with pytest.raises(ValueError, match="missing required keys"):
        await agent.run(job_data={"job_summary": "Python role"}, trace_id=trace_id)


@pytest.mark.asyncio
async def test_run_missing_base_resume_text_raises_file_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, _, _ = _setup_agent(monkeypatch, tmp_path, create_resume_text=False)
    trace_id = uuid4()
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps(_valid_research_payload())}
    )

    with pytest.raises(FileNotFoundError, match="BASE_RESUME_TEXT"):
        await agent.run(job_data={"job_summary": "Python role"}, trace_id=trace_id)


@pytest.mark.asyncio
async def test_run_missing_pipeline_row_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    agent, db_session, _ = _setup_agent(monkeypatch, tmp_path)
    trace_id = uuid4()
    db_session.execute.return_value = SimpleNamespace(rowcount=0)
    agent._call_model = AsyncMock(  # type: ignore[method-assign]
        return_value={"text": json.dumps(_valid_research_payload())}
    )

    with pytest.raises(ValueError, match="pipeline_runs row not found"):
        await agent.run(job_data={"job_summary": "Python role"}, trace_id=trace_id)


