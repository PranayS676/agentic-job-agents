from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

from job_platform.config import clear_settings_cache


ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5432/jobagent_test"


def _async_to_sync_url(database_url: str) -> str:
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    if database_url.startswith("postgres+asyncpg://"):
        return database_url.replace("postgres+asyncpg://", "postgres+psycopg2://", 1)
    return database_url


def _ensure_database_exists(sync_test_url: str) -> None:
    test_url = make_url(sync_test_url)
    database_name = test_url.database or ""
    if not re.fullmatch(r"[A-Za-z0-9_]+", database_name):
        raise RuntimeError(f"Unsafe test database name: {database_name!r}")

    admin_url: URL = test_url.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :db_name"),
                {"db_name": database_name},
            ).scalar_one_or_none()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{database_name}"'))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"PostgreSQL not available for integration tests: {exc}")
    finally:
        admin_engine.dispose()


def _reset_public_schema(sync_test_url: str) -> None:
    engine = create_engine(sync_test_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO postgres"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
    finally:
        engine.dispose()


def _run_alembic(command_name: str) -> None:
    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT_DIR / "alembic"))
    if command_name == "upgrade":
        command.upgrade(config, "head")
        return
    if command_name == "downgrade":
        command.downgrade(config, "base")
        return
    raise ValueError(f"Unsupported Alembic command: {command_name}")


def _set_required_runtime_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    database_url: str,
    test_database_url: str,
) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    skills_dir = tmp_path / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    base_resume_docx = data_dir / "base_resume.docx"
    base_resume_text = data_dir / "base_resume.md"
    credentials = data_dir / "credentials.json"

    base_resume_docx.write_text("placeholder", encoding="utf-8")
    base_resume_text.write_text("placeholder", encoding="utf-8")
    credentials.write_text("{}", encoding="utf-8")

    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "MANAGER_MODEL": "claude-opus-4-6",
        "RESEARCH_MODEL": "claude-sonnet-4-6",
        "RESUME_EDITOR_MODEL": "claude-sonnet-4-6",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": database_url,
        "TEST_DATABASE_URL": test_database_url,
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": "GROUP1@g.us",
        "POLL_INTERVAL_SECONDS": "30",
        "GMAIL_CREDENTIALS_PATH": str(credentials),
        "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        "SENDER_EMAIL": "sender@example.com",
        "SENDER_NAME": "Pranay",
        "MIN_RELEVANCE_SCORE": "6",
        "MIN_ATS_SCORE": "65",
        "MAX_RESUME_EDIT_ITERATIONS": "2",
        "BASE_RESUME_DOCX": str(base_resume_docx),
        "BASE_RESUME_TEXT": str(base_resume_text),
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_migration_upgrade_trigger_and_downgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)

    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url, async_test_url)
    clear_settings_cache()
    _run_alembic("upgrade")

    engine = create_engine(sync_test_url)
    try:
        with engine.connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT tablename
                        FROM pg_tables
                        WHERE schemaname = 'public'
                        """
                    )
                )
            }
            expected = {
                "whatsapp_messages",
                "polling_cursors",
                "pipeline_runs",
                "resume_versions",
                "outbox",
                "candidate_profile",
                "agent_traces",
            }
            assert expected.issubset(tables)

            whatsapp_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'whatsapp_messages'
                        """
                    )
                )
            }
            assert {"source_timestamp", "external_message_id", "ingest_source"}.issubset(
                whatsapp_columns
            )

            cursor_columns = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'public' AND table_name = 'polling_cursors'
                        """
                    )
                )
            }
            assert {
                "group_id",
                "last_successful_message_timestamp",
                "last_poll_started_at",
                "last_poll_completed_at",
                "last_cutoff_timestamp",
                "status",
                "last_error",
                "created_at",
                "updated_at",
            }.issubset(cursor_columns)

            index_names = {
                row[0]
                for row in conn.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = 'public'
                        """
                    )
                )
            }
            assert "uq_whatsapp_group_external_message" in index_names
            assert "idx_polling_cursors_updated_at" in index_names

        # Insert base records in one transaction and commit so updated_at baseline is persisted.
        with engine.begin() as conn:
            message_id = conn.execute(
                text(
                    """
                    INSERT INTO whatsapp_messages (group_id, sender_number, message_text, message_hash)
                    VALUES (:group_id, :sender_number, :message_text, :message_hash)
                    RETURNING id
                    """
                ),
                {
                    "group_id": "GROUP1@g.us",
                    "sender_number": "+15550000001",
                    "message_text": "Python ML role",
                    "message_hash": "hash_step3_test_1",
                },
            ).scalar_one()

            inserted = conn.execute(
                text(
                    """
                    INSERT INTO pipeline_runs (message_id, status)
                    VALUES (:message_id, 'started')
                    RETURNING trace_id, updated_at
                    """
                ),
                {"message_id": str(message_id)},
            ).one()
            trace_id = inserted[0]
            updated_before = inserted[1]

        time.sleep(0.05)

        # Update in a separate transaction so NOW() differs from previous transaction timestamp.
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE pipeline_runs
                    SET status = 'research_done'
                    WHERE trace_id = :trace_id
                    """
                ),
                {"trace_id": str(trace_id)},
            )

        with engine.connect() as conn:
            updated_after = conn.execute(
                text("SELECT updated_at FROM pipeline_runs WHERE trace_id = :trace_id"),
                {"trace_id": str(trace_id)},
            ).scalar_one()
            assert updated_after > updated_before

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO resume_versions (trace_id, version_number)
                    VALUES (:trace_id, 1)
                    """
                ),
                {"trace_id": str(trace_id)},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO outbox (trace_id, channel, recipient)
                    VALUES (:trace_id, 'email', 'poster@example.com')
                    """
                ),
                {"trace_id": str(trace_id)},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO agent_traces (trace_id, agent_name, model)
                    VALUES (:trace_id, 'manager_agent', 'claude-opus-4-6')
                    """
                ),
                {"trace_id": str(trace_id)},
            )
    finally:
        engine.dispose()

    clear_settings_cache()
    _run_alembic("downgrade")

    engine = create_engine(sync_test_url)
    try:
        with engine.connect() as conn:
            remaining = conn.execute(
                text(
                    """
                    SELECT to_regclass('public.whatsapp_messages'),
                           to_regclass('public.polling_cursors'),
                           to_regclass('public.pipeline_runs'),
                           to_regclass('public.resume_versions'),
                           to_regclass('public.outbox'),
                           to_regclass('public.candidate_profile'),
                           to_regclass('public.agent_traces')
                    """
                )
            ).one()
            assert all(value is None for value in remaining)
    finally:
        engine.dispose()



