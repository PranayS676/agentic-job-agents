from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import job_backend.main as main_module
from job_platform.config import clear_settings_cache


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
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
        "PDF_CONVERTER_MODEL": "claude-haiku-4-5-20251001",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/jobagent_test",
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
        "SKILLS_DIR": str(Path.cwd() / "apps" / "agent-runtime" / "skills"),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.asyncio
async def test_async_main_backend_runtime_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _set_required_env(monkeypatch, tmp_path)
    clear_settings_cache()
    args = SimpleNamespace(host="0.0.0.0", port=8000, log_level=None, disable_polling=False)

    monkeypatch.setattr(main_module, "_run_migrations", AsyncMock())
    monkeypatch.setattr(main_module, "_db_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_waha_connectivity_status", AsyncMock(return_value="ok"))
    monkeypatch.setattr(main_module, "_print_readiness_table", lambda **kwargs: None)  # noqa: ARG005
    monkeypatch.setattr(main_module, "create_app", lambda enable_polling: object())  # noqa: ARG005

    class _FakeConfig:
        def __init__(self, app, host, port, log_level):  # noqa: ANN001
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class _FakeServer:
        def __init__(self, config):  # noqa: ANN001
            self.config = config

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(main_module.uvicorn, "Config", _FakeConfig)
    monkeypatch.setattr(main_module.uvicorn, "Server", _FakeServer)

    exit_code = await main_module._async_main(args)
    assert exit_code == 0


