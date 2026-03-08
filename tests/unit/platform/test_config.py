from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from job_platform.config import (
    Settings,
    clear_settings_cache,
    get_settings,
    validate_startup_requirements,
)


ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "MANAGER_MODEL",
    "RESEARCH_MODEL",
    "RESUME_EDITOR_MODEL",
    "PDF_CONVERTER_MODEL",
    "GMAIL_AGENT_MODEL",
    "WHATSAPP_MSG_MODEL",
    "DATABASE_URL",
    "WAHA_BASE_URL",
    "WAHA_SESSION",
    "WAHA_API_KEY",
    "WHATSAPP_GROUP_IDS",
    "POLL_INTERVAL_SECONDS",
    "GMAIL_CREDENTIALS_PATH",
    "GMAIL_TOKEN_PATH",
    "SENDER_EMAIL",
    "SENDER_NAME",
    "MIN_RELEVANCE_SCORE",
    "MIN_ATS_SCORE",
    "MAX_RESUME_EDIT_ITERATIONS",
    "BASE_RESUME_DOCX",
    "BASE_RESUME_TEXT",
    "OUTPUT_DIR",
    "SKILLS_DIR",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "OTEL_ENDPOINT",
]


DEFAULT_ENV = {
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
    "WAHA_API_KEY": "waha-test-key",
    "WHATSAPP_GROUP_IDS": "GROUP1@g.us,GROUP2@g.us,GROUP3@g.us",
    "POLL_INTERVAL_SECONDS": "30",
    "GMAIL_CREDENTIALS_PATH": "data/credentials.json",
    "GMAIL_TOKEN_PATH": "data/token.json",
    "SENDER_EMAIL": "sender@example.com",
    "SENDER_NAME": "Pranay",
    "MIN_RELEVANCE_SCORE": "6",
    "MIN_ATS_SCORE": "65",
    "MAX_RESUME_EDIT_ITERATIONS": "2",
    "BASE_RESUME_DOCX": "data/base_resume.docx",
    "BASE_RESUME_TEXT": "data/base_resume.md",
    "OUTPUT_DIR": "output",
    "SKILLS_DIR": "apps/agent-runtime/skills",
    "LOG_LEVEL": "INFO",
    "LOG_FORMAT": "json",
    "OTEL_ENDPOINT": "http://localhost:4317",
}


def set_env(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str] | None = None) -> None:
    values = dict(DEFAULT_ENV)
    if overrides:
        values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_cache()
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield
    clear_settings_cache()
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_settings_load_from_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    settings = Settings()
    assert settings.manager_model == "claude-opus-4-6"
    assert settings.poll_interval_seconds == 30
    assert settings.log_format == "json"


def test_settings_load_from_dotenv_when_os_env_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    write_env_file(tmp_path / ".env", DEFAULT_ENV)
    settings = Settings()
    assert settings.research_model == "claude-sonnet-4-6"
    assert settings.sender_name == "Pranay"


def test_os_environment_overrides_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv_values = dict(DEFAULT_ENV)
    dotenv_values["MANAGER_MODEL"] = "from-dotenv"
    monkeypatch.chdir(tmp_path)
    write_env_file(tmp_path / ".env", dotenv_values)
    monkeypatch.setenv("MANAGER_MODEL", "from-os-env")
    settings = Settings()
    assert settings.manager_model == "from-os-env"


def test_whatsapp_group_ids_list_parses_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch, {"WHATSAPP_GROUP_IDS": " a@g.us, ,b@g.us,a@g.us , , c@g.us "})
    settings = Settings()
    assert settings.whatsapp_group_ids_list == ["a@g.us", "b@g.us", "c@g.us"]


@pytest.mark.parametrize(
    ("database_url", "expected"),
    [
        (
            "postgresql+asyncpg://postgres:password@localhost:5432/jobagent",
            "postgresql+psycopg2://postgres:password@localhost:5432/jobagent",
        ),
        (
            "postgres+asyncpg://postgres:password@localhost:5432/jobagent",
            "postgres+psycopg2://postgres:password@localhost:5432/jobagent",
        ),
    ],
)
def test_db_url_sync_converts_async_postgres_urls(
    monkeypatch: pytest.MonkeyPatch, database_url: str, expected: str
) -> None:
    set_env(monkeypatch, {"DATABASE_URL": database_url})
    settings = Settings()
    assert settings.db_url_sync == expected


def test_db_url_sync_passthrough_for_sync_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    sync_url = "postgresql+psycopg2://postgres:password@localhost:5432/jobagent"
    set_env(monkeypatch, {"DATABASE_URL": sync_url})
    settings = Settings()
    assert settings.db_url_sync == sync_url


def test_db_url_sync_rejects_non_postgres_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, {"DATABASE_URL": "sqlite:///tmp/test.db"})
    settings = Settings()
    with pytest.raises(ValueError, match="PostgreSQL scheme"):
        _ = settings.db_url_sync


@pytest.mark.parametrize(
    ("overrides", "field_name"),
    [
        ({"POLL_INTERVAL_SECONDS": "0"}, "poll_interval_seconds"),
        ({"MIN_RELEVANCE_SCORE": "11"}, "min_relevance_score"),
        ({"MIN_ATS_SCORE": "101"}, "min_ats_score"),
        ({"MAX_RESUME_EDIT_ITERATIONS": "0"}, "max_resume_edit_iterations"),
    ],
)
def test_numeric_bounds_raise_validation_errors(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], field_name: str
) -> None:
    set_env(monkeypatch, overrides)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert field_name in str(exc_info.value)


def test_whatsapp_group_ids_empty_after_trim_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_env(monkeypatch, {"WHATSAPP_GROUP_IDS": " , , "})
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "whatsapp_group_ids" in str(exc_info.value)


def test_validate_startup_requirements_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skills_dir = tmp_path / "skills"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "data"
    skills_dir.mkdir()
    output_dir.mkdir()
    data_dir.mkdir()

    base_resume_docx = data_dir / "base_resume.docx"
    base_resume_text = data_dir / "base_resume.md"
    gmail_credentials = data_dir / "credentials.json"
    base_resume_docx.write_text("docx-placeholder", encoding="utf-8")
    base_resume_text.write_text("resume text", encoding="utf-8")
    gmail_credentials.write_text("{}", encoding="utf-8")

    set_env(
        monkeypatch,
        {
            "SKILLS_DIR": str(skills_dir),
            "OUTPUT_DIR": str(output_dir),
            "BASE_RESUME_DOCX": str(base_resume_docx),
            "BASE_RESUME_TEXT": str(base_resume_text),
            "GMAIL_CREDENTIALS_PATH": str(gmail_credentials),
            "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        },
    )
    settings = Settings()
    validate_startup_requirements(settings)


def test_validate_startup_requirements_reports_all_missing_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_skills_dir = tmp_path / "missing-skills"
    missing_output_dir = tmp_path / "missing-output"
    missing_docx = tmp_path / "missing" / "base_resume.docx"
    missing_text = tmp_path / "missing" / "base_resume.md"
    missing_credentials = tmp_path / "missing" / "credentials.json"

    set_env(
        monkeypatch,
        {
            "SKILLS_DIR": str(missing_skills_dir),
            "OUTPUT_DIR": str(missing_output_dir),
            "BASE_RESUME_DOCX": str(missing_docx),
            "BASE_RESUME_TEXT": str(missing_text),
            "GMAIL_CREDENTIALS_PATH": str(missing_credentials),
        },
    )
    settings = Settings()
    with pytest.raises(RuntimeError) as exc_info:
        validate_startup_requirements(settings)

    error_text = str(exc_info.value)
    assert str(missing_skills_dir) in error_text
    assert str(missing_output_dir) in error_text
    assert str(missing_docx) in error_text
    assert str(missing_text) in error_text
    assert str(missing_credentials) in error_text


def test_missing_gmail_token_file_does_not_fail_startup_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skills_dir = tmp_path / "skills"
    output_dir = tmp_path / "output"
    data_dir = tmp_path / "data"
    skills_dir.mkdir()
    output_dir.mkdir()
    data_dir.mkdir()

    base_resume_docx = data_dir / "base_resume.docx"
    base_resume_text = data_dir / "base_resume.md"
    gmail_credentials = data_dir / "credentials.json"
    missing_token = data_dir / "token.json"

    base_resume_docx.write_text("docx-placeholder", encoding="utf-8")
    base_resume_text.write_text("resume text", encoding="utf-8")
    gmail_credentials.write_text("{}", encoding="utf-8")

    set_env(
        monkeypatch,
        {
            "SKILLS_DIR": str(skills_dir),
            "OUTPUT_DIR": str(output_dir),
            "BASE_RESUME_DOCX": str(base_resume_docx),
            "BASE_RESUME_TEXT": str(base_resume_text),
            "GMAIL_CREDENTIALS_PATH": str(gmail_credentials),
            "GMAIL_TOKEN_PATH": str(missing_token),
        },
    )
    settings = Settings()
    validate_startup_requirements(settings)


def test_get_settings_returns_cached_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch)
    clear_settings_cache()
    first = get_settings()
    second = get_settings()
    assert first is second


def test_clear_settings_cache_forces_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    set_env(monkeypatch, {"MANAGER_MODEL": "model-a"})
    clear_settings_cache()
    first = get_settings()
    assert first.manager_model == "model-a"

    monkeypatch.setenv("MANAGER_MODEL", "model-b")
    second = get_settings()
    assert second is first
    assert second.manager_model == "model-a"

    clear_settings_cache()
    third = get_settings()
    assert third.manager_model == "model-b"
    assert third is not first


