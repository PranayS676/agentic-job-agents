from __future__ import annotations

import asyncio
import importlib
import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import clear_mappers

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
        pytest.skip(f"PostgreSQL not available for ingest integration tests: {exc}")
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
    group_ids: str,
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
        "TEST_DATABASE_URL": database_url,
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": group_ids,
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


def test_ingest_webhook_inserts_deduplicates_and_filters_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url, "group-1@g.us, group-1@g.us")
    clear_settings_cache()
    _run_alembic("upgrade")
    upgraded = True

    database_module = None
    try:
        clear_mappers()
        import job_platform.database as database_module
        import job_platform.models as models_module
        import job_backend.services.ingest as ingest_module

        importlib.reload(database_module)
        importlib.reload(models_module)
        importlib.reload(ingest_module)

        app = ingest_module.create_app(enable_polling=False)

        with TestClient(app) as client:
            payload = {
                "chatId": "group-1@g.us",
                "fromNumber": "15550001111",
                "text": "Backend Python role with FastAPI",
                "timestamp": 1710000000,
                "id": "wa-msg-1",
            }
            response = client.post("/webhook/waha", json=payload)
            assert response.status_code == 200
            assert response.json() == {"status": "processed"}

            duplicate = client.post("/webhook/waha", json=payload)
            assert duplicate.status_code == 200
            assert duplicate.json() == {"status": "duplicate_ignored"}

            ignored = client.post(
                "/webhook/waha",
                json={
                    "chatId": "group-2@g.us",
                    "fromNumber": "15550001111",
                    "text": "Should be filtered",
                },
            )
            assert ignored.status_code == 202
            assert ignored.json() == {"status": "ignored_group"}

            health = client.get("/health")
            assert health.status_code == 200
            health_payload = health.json()
            assert health_payload["status"] == "ok"
            assert health_payload["groups_monitored"] == 1
            assert health_payload["polling_enabled"] is False
            assert health_payload["last_poll_started_at"] is None
            assert health_payload["last_poll_completed_at"] is None
            assert health_payload["polling_status"] == "idle"

        engine = create_engine(sync_test_url)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        """
                        SELECT group_id,
                               sender_number,
                               message_text,
                               source_timestamp,
                               external_message_id,
                               ingest_source,
                               processed
                        FROM whatsapp_messages
                        ORDER BY created_at ASC
                        """
                    )
                ).all()
                assert len(rows) == 1
                assert rows[0][0] == "group-1@g.us"
                assert rows[0][1] == "15550001111"
                assert rows[0][2] == "Backend Python role with FastAPI"
                assert rows[0][3] == 1710000000
                assert rows[0][4] == "wa-msg-1"
                assert rows[0][5] == "webhook"
                assert rows[0][6] is False
        finally:
            engine.dispose()
    finally:
        clear_mappers()
        if database_module is not None:
            asyncio.run(database_module.engine.dispose())
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")



