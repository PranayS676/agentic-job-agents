from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from job_agent_runtime.agents.pdf_converter_agent import PDFConverterAgent
from job_platform.config import clear_settings_cache


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = tmp_path / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "base_resume.docx").write_text("docx", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("resume", encoding="utf-8")
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
    skill_dir = tmp_path / "skills" / "pdf-converter"
    scripts_dir = skill_dir / "scripts"
    references_dir = skill_dir / "references"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    references_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("# PDF Converter", encoding="utf-8")
    (scripts_dir / "convert.py").write_text("print('{}')", encoding="utf-8")


def _build_agent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PDFConverterAgent:
    _set_required_env(monkeypatch, tmp_path)
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
    return PDFConverterAgent(db_session=db_session, tracer=tracer)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_settings_cache()
    yield
    clear_settings_cache()


@pytest.mark.asyncio
async def test_run_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = _build_agent(monkeypatch, tmp_path)
    docx_path = tmp_path / "resume.docx"
    docx_path.write_text("docx", encoding="utf-8")
    output_pdf = Path(agent.settings.output_dir) / "pdfs" / "resume.pdf"
    output_pdf.parent.mkdir(parents=True, exist_ok=True)
    output_pdf.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(
        "job_agent_runtime.agents.pdf_converter_agent.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(  # noqa: ARG005
            returncode=0,
            stdout=json.dumps({"pdf_path": str(output_pdf), "status": "success", "error": None}),
            stderr="",
        ),
    )

    result = await agent.run(input_data={"docx_path": str(docx_path)}, trace_id=uuid4())
    assert result["status"] == "success"
    assert result["pdf_path"] == str(output_pdf)


@pytest.mark.asyncio
async def test_run_missing_docx_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = _build_agent(monkeypatch, tmp_path)
    with pytest.raises(FileNotFoundError, match="docx_path not found"):
        await agent.run(input_data={"docx_path": str(tmp_path / "missing.docx")}, trace_id=uuid4())


@pytest.mark.asyncio
async def test_run_converter_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    agent = _build_agent(monkeypatch, tmp_path)
    docx_path = tmp_path / "resume.docx"
    docx_path.write_text("docx", encoding="utf-8")

    monkeypatch.setattr(
        "job_agent_runtime.agents.pdf_converter_agent.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="no binary"),  # noqa: ARG005
    )

    with pytest.raises(RuntimeError, match="PDF conversion failed"):
        await agent.run(input_data={"docx_path": str(docx_path)}, trace_id=uuid4())


