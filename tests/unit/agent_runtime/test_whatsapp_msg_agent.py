from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from job_agent_runtime.agents.whatsapp_msg_agent import WhatsAppMsgAgent
from job_platform.config import clear_settings_cache, get_settings


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = Path.cwd() / "apps" / "agent-runtime" / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "resumes").mkdir(parents=True, exist_ok=True)

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
async def test_whatsapp_agent_run_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    connector = SimpleNamespace(
        send_message_with_file=AsyncMock(return_value={"ok": True, "data": {"id": "waha-msg-1"}}),
        close=AsyncMock(),
    )
    agent = WhatsAppMsgAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=connector,
    )
    agent._call_model = AsyncMock(return_value={"text": '{"message_text":"Hi from agent"}'})  # type: ignore[method-assign]

    attachment_path = tmp_path / "output" / "resumes" / "resume.docx"
    attachment_path.write_text("docx", encoding="utf-8")

    result = await agent.run(
        context={
            "job_title": "ML Engineer",
            "company": "Acme",
            "job_summary": "Python and ML",
            "poster_number": "+15550001111",
            "attachment_path": str(attachment_path),
        },
        trace_id=uuid4(),
    )

    assert result["sent"] is True
    assert result["channel"] == "whatsapp"
    assert result["recipient"] == "+15550001111"
    assert result["external_id"] == "waha-msg-1"
    connector.send_message_with_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_whatsapp_agent_send_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_required_env(monkeypatch, tmp_path)

    import job_agent_runtime.agents.base_agent as base_agent_module

    monkeypatch.setattr(base_agent_module, "AsyncAnthropic", lambda *args, **kwargs: SimpleNamespace())  # noqa: ARG005

    settings = get_settings()
    connector = SimpleNamespace(
        send_message_with_file=AsyncMock(return_value={"ok": False, "error": "not_connected"}),
        close=AsyncMock(),
    )
    agent = WhatsAppMsgAgent(
        db_session=SimpleNamespace(),
        tracer=SimpleNamespace(trace=AsyncMock(), update_pipeline_status=AsyncMock()),
        settings=settings,
        connector=connector,
    )
    agent._call_model = AsyncMock(return_value={"text": '{"message_text":"Hi from agent"}'})  # type: ignore[method-assign]

    attachment_path = tmp_path / "output" / "resumes" / "resume.docx"
    attachment_path.write_text("docx", encoding="utf-8")

    with pytest.raises(RuntimeError, match="WAHA send_message_with_file failed"):
        await agent.run(
            context={
                "job_title": "ML Engineer",
                "company": "Acme",
                "job_summary": "Python and ML",
                "poster_number": "+15550001111",
                "attachment_path": str(attachment_path),
            },
            trace_id=uuid4(),
        )


