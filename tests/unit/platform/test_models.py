from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID

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
def metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _set_required_env(monkeypatch, tmp_path)
    clear_settings_cache()

    import job_platform.models as models

    yield models.Base.metadata
    clear_settings_cache()


def test_all_required_tables_exist(metadata) -> None:
    expected = {
        "whatsapp_messages",
        "pipeline_runs",
        "polling_cursors",
        "resume_versions",
        "outbox",
        "candidate_profile",
        "agent_traces",
    }
    assert expected.issubset(set(metadata.tables.keys()))


def test_key_columns_constraints_and_indexes(metadata) -> None:
    whatsapp = metadata.tables["whatsapp_messages"]
    pipeline = metadata.tables["pipeline_runs"]
    polling_cursors = metadata.tables["polling_cursors"]
    traces = metadata.tables["agent_traces"]

    assert "message_hash" in whatsapp.c
    assert whatsapp.c.message_hash.unique is True
    assert "source_timestamp" in whatsapp.c
    assert "external_message_id" in whatsapp.c
    assert "ingest_source" in whatsapp.c

    whatsapp_indexes = {idx.name for idx in whatsapp.indexes}
    pipeline_indexes = {idx.name for idx in pipeline.indexes}
    polling_indexes = {idx.name for idx in polling_cursors.indexes}
    traces_indexes = {idx.name for idx in traces.indexes}

    assert "idx_whatsapp_unprocessed" in whatsapp_indexes
    assert "uq_whatsapp_group_external_message" in whatsapp_indexes
    assert "idx_pipeline_status" in pipeline_indexes
    assert "idx_polling_cursors_updated_at" in polling_indexes
    assert "idx_traces_trace_id" in traces_indexes


def test_postgres_specific_types_present(metadata) -> None:
    whatsapp = metadata.tables["whatsapp_messages"]
    pipeline = metadata.tables["pipeline_runs"]
    polling_cursors = metadata.tables["polling_cursors"]
    profile = metadata.tables["candidate_profile"]
    traces = metadata.tables["agent_traces"]

    assert isinstance(whatsapp.c.id.type, PGUUID)
    assert isinstance(whatsapp.c.source_timestamp.type, BigInteger)
    assert isinstance(polling_cursors.c.last_successful_message_timestamp.type, BigInteger)
    assert isinstance(pipeline.c.manager_decision.type, JSONB)
    assert isinstance(pipeline.c.research_output.type, JSONB)
    assert isinstance(traces.c.full_input.type, JSONB)
    assert isinstance(profile.c.target_roles.type, ARRAY)
    assert isinstance(profile.c.target_stack.type, ARRAY)


def test_foreign_keys_exist(metadata) -> None:
    pipeline = metadata.tables["pipeline_runs"]
    resume_versions = metadata.tables["resume_versions"]
    outbox = metadata.tables["outbox"]
    traces = metadata.tables["agent_traces"]

    pipeline_fk_targets = {fk.target_fullname for fk in pipeline.c.message_id.foreign_keys}
    resume_fk_targets = {fk.target_fullname for fk in resume_versions.c.trace_id.foreign_keys}
    outbox_fk_targets = {fk.target_fullname for fk in outbox.c.trace_id.foreign_keys}
    trace_fk_targets = {fk.target_fullname for fk in traces.c.trace_id.foreign_keys}

    assert "whatsapp_messages.id" in pipeline_fk_targets
    assert "pipeline_runs.trace_id" in resume_fk_targets
    assert "pipeline_runs.trace_id" in outbox_fk_targets
    assert "pipeline_runs.trace_id" in trace_fk_targets


