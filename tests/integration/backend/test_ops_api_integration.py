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
        pytest.skip(f"PostgreSQL not available for backend ops integration tests: {exc}")
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


def test_ops_endpoints_expose_backend_dashboard_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url, "group-1@g.us,group-2@g.us")
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

        engine = create_engine(sync_test_url)
        try:
            with engine.begin() as conn:
                unprocessed_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed
                        )
                        VALUES ('group-1@g.us', '15550000001', 'pending message', 'ops_hash_pending', false)
                        RETURNING id
                        """
                    )
                ).scalar_one()
                assert unprocessed_message_id is not None

                sent_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed
                        )
                        VALUES ('group-1@g.us', '15550000002', 'sent message', 'ops_hash_sent', true)
                        RETURNING id
                        """
                    )
                ).scalar_one()
                review_email_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed
                        )
                        VALUES ('group-1@g.us', '15550000003', 'review email message', 'ops_hash_review_email', true)
                        RETURNING id
                        """
                    )
                ).scalar_one()
                review_whatsapp_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed
                        )
                        VALUES ('group-2@g.us', '15550000004', 'review whatsapp message', 'ops_hash_review_whatsapp', true)
                        RETURNING id
                        """
                    )
                ).scalar_one()
                failed_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed, processing_error
                        )
                        VALUES ('group-2@g.us', '15550000005', 'failed message', 'ops_hash_failed', true, 'send failed')
                        RETURNING id
                        """
                    )
                ).scalar_one()
                discarded_message_id = conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed
                        )
                        VALUES ('group-2@g.us', '15550000006', 'discarded message', 'ops_hash_discarded', true)
                        RETURNING id
                        """
                    )
                ).scalar_one()

                sent_trace_id = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status, job_title, company, job_summary)
                        VALUES (:message_id, 'sent', 'ML Engineer', 'Acme', 'sent role')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": sent_message_id},
                ).scalar_one()
                review_email_trace_id = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status, job_title, company, job_summary)
                        VALUES (:message_id, 'review_required', 'Cloud Data Engineer', 'Acme', 'review email role')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": review_email_message_id},
                ).scalar_one()
                review_whatsapp_trace_id = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status, job_title, company, job_summary)
                        VALUES (:message_id, 'review_required', 'Platform Engineer', 'Acme', 'review whatsapp role')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": review_whatsapp_message_id},
                ).scalar_one()
                failed_trace_id = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status, job_title, company, job_summary, error_stage, error_message)
                        VALUES (:message_id, 'failed', 'ML Engineer', 'Acme', 'failed role', 'routing', 'transport failed')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": failed_message_id},
                ).scalar_one()
                discarded_trace_id = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status, job_title, company, job_summary)
                        VALUES (:message_id, 'discarded', 'Unknown Title', 'Unknown Company', 'discarded role')
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": discarded_message_id},
                ).scalar_one()

                conn.execute(
                    text(
                        """
                        INSERT INTO resume_versions (trace_id, version_number, docx_path, attachment_path)
                        VALUES
                          (:sent_trace_id, 1, 'output/resumes/sent.docx', 'output/resumes/sent.docx'),
                          (:review_email_trace_id, 1, 'output/resumes/review_email.docx', 'output/resumes/review_email.docx'),
                          (:review_whatsapp_trace_id, 1, 'output/resumes/review_whatsapp.docx', 'output/resumes/review_whatsapp.docx')
                        """
                    ),
                    {
                        "sent_trace_id": sent_trace_id,
                        "review_email_trace_id": review_email_trace_id,
                        "review_whatsapp_trace_id": review_whatsapp_trace_id,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO outbox (trace_id, channel, recipient, subject, body_preview, attachment_path, status)
                        VALUES
                          (:sent_trace_id, 'email', 'recruiter@example.com', 'Sent role', 'sent body', 'output/resumes/sent.docx', 'sent'),
                          (:review_email_trace_id, 'email', 'reviewer@example.com', 'Review role', 'review email body', 'output/resumes/review_email.docx', 'review_required'),
                          (:review_whatsapp_trace_id, 'whatsapp', '+15550001234', NULL, 'review whatsapp body', 'output/resumes/review_whatsapp.docx', 'review_required'),
                          (:failed_trace_id, 'whatsapp', '+15550001111', NULL, 'failed body', 'output/resumes/failed.docx', 'failed')
                        """
                    ),
                    {
                        "sent_trace_id": sent_trace_id,
                        "review_email_trace_id": review_email_trace_id,
                        "review_whatsapp_trace_id": review_whatsapp_trace_id,
                        "failed_trace_id": failed_trace_id,
                    },
                )

                conn.execute(
                    text(
                        """
                        INSERT INTO polling_cursors (
                            group_id,
                            last_successful_message_timestamp,
                            last_poll_started_at,
                            last_poll_completed_at,
                            last_cutoff_timestamp,
                            status,
                            last_error
                        )
                        VALUES
                          ('group-1@g.us', 1710000000, NOW() - INTERVAL '2 minutes', NOW() - INTERVAL '1 minute', 1710000050, 'ok', NULL),
                          ('group-2@g.us', 1710000100, NOW() - INTERVAL '5 minutes', NOW() - INTERVAL '4 minutes', 1710000150, 'error', 'connector timeout')
                        """
                    )
                )
                assert discarded_trace_id is not None
        finally:
            engine.dispose()

        app = ingest_module.create_app(enable_polling=False)
        with TestClient(app) as client:
            overview = client.get("/api/ops/overview")
            assert overview.status_code == 200
            overview_payload = overview.json()
            assert overview_payload["groups_monitored"] == 2
            assert overview_payload["polling_enabled"] is False
            assert overview_payload["polling_status"] == "error"
            assert overview_payload["unprocessed_messages_count"] == 1
            assert overview_payload["review_required_count"] == 2
            assert overview_payload["failed_pipeline_count"] == 1
            assert overview_payload["sent_pipeline_count_24h"] == 1
            assert overview_payload["discarded_pipeline_count_24h"] == 1
            assert overview_payload["review_required_count_24h"] == 2
            assert overview_payload["failed_count_24h"] == 1
            assert overview_payload["last_poll_started_at"] is not None
            assert overview_payload["last_poll_completed_at"] is not None

            review_queue = client.get("/api/ops/review-queue", params={"limit": 10})
            assert review_queue.status_code == 200
            review_queue_payload = review_queue.json()
            assert len(review_queue_payload) == 2
            assert {row["status"] for row in review_queue_payload} == {"review_required"}
            assert {row["channel"] for row in review_queue_payload} == {"email", "whatsapp"}
            assert {row["attachment_path"] for row in review_queue_payload} == {
                "output/resumes/review_email.docx",
                "output/resumes/review_whatsapp.docx",
            }

            pipeline_runs = client.get("/api/ops/pipeline-runs", params={"limit": 10})
            assert pipeline_runs.status_code == 200
            pipeline_payload = pipeline_runs.json()
            assert len(pipeline_payload) == 5
            assert {row["status"] for row in pipeline_payload} == {
                "sent",
                "review_required",
                "failed",
                "discarded",
            }

            filtered_runs = client.get(
                "/api/ops/pipeline-runs",
                params={"status": "review_required", "limit": 10},
            )
            assert filtered_runs.status_code == 200
            filtered_payload = filtered_runs.json()
            assert len(filtered_payload) == 2
            assert all(row["status"] == "review_required" for row in filtered_payload)

            polling_status = client.get("/api/ops/polling-status")
            assert polling_status.status_code == 200
            polling_payload = polling_status.json()
            assert len(polling_payload) == 2
            assert polling_payload[0]["group_id"] == "group-1@g.us"
            assert polling_payload[1]["group_id"] == "group-2@g.us"
            assert polling_payload[0]["status"] == "ok"
            assert polling_payload[1]["status"] == "error"
            assert polling_payload[1]["last_error"] == "connector timeout"
    finally:
        clear_mappers()
        if database_module is not None:
            asyncio.run(database_module.engine.dispose())
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")
