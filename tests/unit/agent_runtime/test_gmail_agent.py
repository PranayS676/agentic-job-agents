from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from job_agent_runtime.agents.gmail_agent import GmailAgent
from job_platform.config import clear_settings_cache, get_settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = Path.cwd() / "apps" / "agent-runtime" / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("resume", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")

    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "MANAGER_MODEL": "claude-opus-4-6",
        "RESEARCH_MODEL": "claude-sonnet-4-6",
        "RESUME_EDITOR_MODEL": "claude-sonnet-4-6",
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


@pytest.fixture(autouse=True)
def _clear_settings_cache_fixture():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.asyncio
async def test_gmail_agent_run_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    connector = SimpleNamespace(send=AsyncMock(return_value="gmail-msg-1"))
    agent = GmailAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=connector,
    )
    agent._call_model = AsyncMock(return_value={"text": '{"subject":"Apply","body":"Hello there"}'})  # type: ignore[method-assign]

    result = await agent.run(
        context={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML contract role on W2 or C2C",
            "poster_email": "recruiter@example.com",
            "attachment_path": "output/resumes/resume.docx",
            "relevance_decision": "fit",
        },
        trace_id=uuid4(),
    )

    assert result["sent"] is True
    assert result["channel"] == "email"
    assert result["recipient"] == "recruiter@example.com"
    assert result["external_id"] == "gmail-msg-1"
    connector.send.assert_awaited_once()
    prompt = agent._call_model.await_args.kwargs["messages"][-1]["content"]  # type: ignore[attr-defined]
    assert "delivery_mode: send" in prompt
    assert "mentions_contract" in prompt
    assert "mentions_w2" in prompt


@pytest.mark.asyncio
async def test_gmail_agent_draft_mode_does_not_send(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    connector = SimpleNamespace(send=AsyncMock(return_value="gmail-msg-1"))
    agent = GmailAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=connector,
    )
    agent._call_model = AsyncMock(return_value={"text": '{"subject":"Apply","body":"Hello there"}'})  # type: ignore[method-assign]

    result = await agent.run(
        context={
            "job_title": "Platform Engineer",
            "company": "Acme",
            "job_summary": "Project-based platform role on W2",
            "poster_email": "recruiter@example.com",
            "attachment_path": "output/resumes/resume.docx",
            "relevance_decision": "okayish",
        },
        trace_id=uuid4(),
        delivery_mode="draft",
    )

    assert result["sent"] is False
    assert result["external_id"] is None
    connector.send.assert_not_awaited()
    prompt = agent._call_model.await_args.kwargs["messages"][-1]["content"]  # type: ignore[attr-defined]
    assert "delivery_mode: draft" in prompt


@pytest.mark.asyncio
async def test_gmail_agent_missing_email(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    agent = GmailAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=SimpleNamespace(send=AsyncMock()),
    )

    with pytest.raises(ValueError, match="poster_email is required"):
        await agent.run(
            context={"job_title": "ML Engineer"},
            trace_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_gmail_agent_missing_attachment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    agent = GmailAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=SimpleNamespace(send=AsyncMock()),
    )

    with pytest.raises(ValueError, match="attachment_path is required"):
        await agent.run(
            context={
                "job_title": "ML Engineer",
                "poster_email": "recruiter@example.com",
            },
            trace_id=uuid4(),
        )


