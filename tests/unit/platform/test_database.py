from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

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


@pytest.fixture
def database_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_required_env(monkeypatch, tmp_path)
    clear_settings_cache()
    import job_platform.database as database

    database = importlib.reload(database)
    yield database
    clear_settings_cache()


def test_engine_and_session_factory_initialize(database_module) -> None:
    assert database_module.engine is not None
    assert database_module.AsyncSessionLocal is not None
    assert database_module.Base is not None


@pytest.mark.asyncio
async def test_get_session_yields_async_session(database_module) -> None:
    async with database_module.get_session() as session:
        assert isinstance(session, AsyncSession)


@pytest.mark.asyncio
async def test_get_session_rolls_back_on_exception(database_module, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.rollback_called = False
            self.close_called = False

        async def rollback(self) -> None:
            self.rollback_called = True

        async def close(self) -> None:
            self.close_called = True

    fake = FakeSession()
    monkeypatch.setattr(database_module, "AsyncSessionLocal", lambda: fake)

    with pytest.raises(RuntimeError, match="boom"):
        async with database_module.get_session():
            raise RuntimeError("boom")

    assert fake.rollback_called is True
    assert fake.close_called is True


