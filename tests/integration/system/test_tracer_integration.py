from __future__ import annotations

import os
import re
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from job_platform.config import clear_settings_cache
from job_platform.tracer import AgentTracer


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


@pytest.mark.asyncio
async def test_tracer_writes_traces_and_pipeline_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url, async_test_url)

    clear_settings_cache()
    _run_alembic("upgrade")
    upgraded = True

    async_engine = create_async_engine(async_test_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with session_factory() as session:
            message_id = (
                await session.execute(
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
                        "message_text": "Python role available",
                        "message_hash": "trace_integration_hash_1",
                    },
                )
            ).scalar_one()

            trace_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status)
                        VALUES (:message_id, 'started')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": str(message_id)},
                )
            ).scalar_one()
            assert isinstance(trace_id, UUID)

            tracer = AgentTracer(session)
            await tracer.trace(
                trace_id=trace_id,
                agent_name="ResearchAgent",
                model="claude-sonnet-4-6",
                input_data={"job_summary": "Python role"},
                output_data={"add_items": ["RAG"]},
                tokens_in=120,
                tokens_out=80,
                latency_ms=350,
                decision_summary="Found relevant additions",
            )
            await tracer.update_pipeline_status(
                trace_id=trace_id,
                status="research_done",
                stage_data={"step": "research", "score": 76},
            )
            await tracer.update_pipeline_status(
                trace_id=trace_id,
                status="resume_ready",
                stage_data={"step": "resume_editor", "version": 1},
            )
            await session.commit()

        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.connect() as conn:
                trace_row = conn.execute(
                    text(
                        """
                        SELECT agent_name, model, input_tokens, output_tokens, decision
                        FROM agent_traces
                        WHERE trace_id = :trace_id
                        """
                    ),
                    {"trace_id": str(trace_id)},
                ).one()
                assert trace_row[0] == "ResearchAgent"
                assert trace_row[1] == "claude-sonnet-4-6"
                assert trace_row[2] == 120
                assert trace_row[3] == 80

                status_row = conn.execute(
                    text(
                        """
                        SELECT status, manager_decision
                        FROM pipeline_runs
                        WHERE trace_id = :trace_id
                        """
                    ),
                    {"trace_id": str(trace_id)},
                ).one()
                assert status_row[0] == "resume_ready"
                manager_decision = status_row[1]
                assert isinstance(manager_decision, dict)
                events = manager_decision.get("events", [])
                assert len(events) == 2
                assert events[0]["status"] == "research_done"
                assert events[1]["status"] == "resume_ready"
                assert "ts" in events[0]
                assert "ts" in events[1]
        finally:
            sync_engine.dispose()
    finally:
        await async_engine.dispose()
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")



