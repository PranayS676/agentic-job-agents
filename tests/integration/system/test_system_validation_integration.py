from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import clear_mappers

from job_agent_runtime.agents.base_agent import BaseAgent
from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory, StubWhatsAppMsgAgent
from job_platform.config import clear_settings_cache, get_settings


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
        pytest.skip(f"PostgreSQL not available for Step 8 integration tests: {exc}")
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
) -> None:
    data_dir = tmp_path / "data"
    output_dir = tmp_path / "output"
    resume_library_dir = data_dir / "resume-library"
    resume_tracks_dir = data_dir / "resume-tracks"
    skills_dir = ROOT_DIR / "apps" / "agent-runtime" / "skills"
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    resume_library_dir.mkdir(parents=True, exist_ok=True)
    resume_tracks_dir.mkdir(parents=True, exist_ok=True)

    base_resume_docx = data_dir / "base_resume.docx"
    base_resume_text = data_dir / "base_resume.md"
    credentials = data_dir / "credentials.json"
    base_resume_docx.write_text("placeholder", encoding="utf-8")
    base_resume_text.write_text("placeholder", encoding="utf-8")
    credentials.write_text("{}", encoding="utf-8")
    track_payload = {
        "track_id": "resume_track_python_ml",
        "source_pdf_path": str(resume_library_dir / "resume_track_python_ml.pdf"),
        "display_name": "Python ML Track",
        "raw_text": "Summary\nPython ML engineer\nSkills\nPython AWS LLM\nExperience\nRecent ML role",
        "normalized_text": "Summary\nPython ML engineer\nSkills\nPython AWS LLM\nExperience\nRecent ML role",
        "sections": {
            "summary": "Python ML engineer",
            "skills": "Python\nAWS\nLLM",
            "experience_recent_role": "Built ML services on AWS.",
            "education": "MS Computer Science",
        },
        "role_bias": ["ai_ml", "backend_python", "cloud_platform"],
        "keywords": ["python", "aws", "llm", "machine learning"],
    }
    for suffix in ("python_ml", "data_platform", "general_backend"):
        payload = dict(track_payload)
        payload["track_id"] = f"resume_track_{suffix}"
        payload["source_pdf_path"] = str(resume_library_dir / f"resume_track_{suffix}.pdf")
        (resume_tracks_dir / f"resume_track_{suffix}.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )

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
        "WHATSAPP_GROUP_IDS": "GROUP1@g.us",
        "POLL_INTERVAL_SECONDS": "1800",
        "GMAIL_CREDENTIALS_PATH": str(credentials),
        "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        "SENDER_EMAIL": "sender@example.com",
        "SENDER_NAME": "Pranay",
        "MIN_RELEVANCE_SCORE": "6",
        "MIN_ATS_SCORE": "65",
        "MAX_RESUME_EDIT_ITERATIONS": "2",
        "BASE_RESUME_DOCX": str(base_resume_docx),
        "BASE_RESUME_TEXT": str(base_resume_text),
        "RESUME_LIBRARY_DIR": str(resume_library_dir),
        "RESUME_TRACKS_DIR": str(resume_tracks_dir),
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class _PolicyAwareWhatsAppAgent(StubWhatsAppMsgAgent):
    async def run(self, context: dict, trace_id, delivery_mode: str = "send"):  # noqa: ANN001
        if delivery_mode == "draft":
            return {
                "sent": False,
                "channel": "whatsapp",
                "recipient": str(context["poster_number"]),
                "subject": None,
                "body_preview": "Draft WhatsApp review body",
                "attachment_path": context.get("attachment_path"),
                "external_id": None,
            }

        if str(context.get("poster_number")) == "+15550001111":
            return {
                "sent": False,
                "channel": "whatsapp",
                "recipient": str(context["poster_number"]),
                "subject": None,
                "body_preview": "WAHA send failed",
                "attachment_path": context.get("attachment_path"),
                "external_id": None,
            }

        return await super().run(context=context, trace_id=trace_id, delivery_mode=delivery_mode)


class _PolicyAwareRoutingFactory(DefaultStubAgentFactory):
    def __init__(self, settings) -> None:  # noqa: ANN001
        super().__init__(settings=settings)
        self._whatsapp = _PolicyAwareWhatsAppAgent()


@pytest.mark.asyncio
async def test_backend_ingest_then_agent_runtime_drain_matches_ops_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url)

    clear_settings_cache()
    _run_alembic("upgrade")

    clear_mappers()
    import job_platform.database as database_module
    import job_platform.models as models_module
    import job_backend.services.ingest as ingest_module
    import job_agent_runtime.orchestration.manager as manager_module
    import job_agent_runtime.worker.watcher as watcher_module

    importlib.reload(database_module)
    importlib.reload(models_module)
    importlib.reload(ingest_module)
    importlib.reload(manager_module)
    importlib.reload(watcher_module)

    async_engine = create_async_engine(async_test_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _fake_call_model(self, messages, trace_id, tools=None, max_tokens=2048):  # noqa: ANN001, ARG002
        content = str(messages[-1].get("content", "")).lower()
        if "evaluate whether this whatsapp message" in content:
            if "discard marketing" in content:
                payload = {
                    "decision": "reject",
                    "decision_score": 0.0,
                    "relevant": False,
                    "score": 2,
                    "job_title": "Unknown Title",
                    "company": "Unknown Company",
                    "job_summary": "Non-technical marketing role",
                    "poster_email": None,
                    "poster_number": "+15550009999",
                    "discard_reason": "Non-technical role",
                    "relevance_reason": "Outside target profile",
                }
            elif "okayish email" in content:
                payload = {
                    "decision": "okayish",
                    "decision_score": 0.5,
                    "relevant": True,
                    "score": 6,
                    "job_title": "Cloud Data Engineer",
                    "company": "Acme",
                    "job_summary": "Okayish W2 cloud data role reviewer@example.com",
                    "poster_email": "reviewer@example.com",
                    "poster_number": "+15550004444",
                    "discard_reason": None,
                    "relevance_reason": "Adjacent cloud/data fit",
                }
            elif "okayish whatsapp" in content:
                payload = {
                    "decision": "okayish",
                    "decision_score": 0.5,
                    "relevant": True,
                    "score": 6,
                    "job_title": "Platform Engineer",
                    "company": "Acme",
                    "job_summary": "Okayish platform role no email",
                    "poster_email": None,
                    "poster_number": "+15550002222",
                    "discard_reason": None,
                    "relevance_reason": "Adjacent platform fit",
                }
            elif "fit whatsapp fail" in content:
                payload = {
                    "decision": "fit",
                    "decision_score": 1.0,
                    "relevant": True,
                    "score": 8,
                    "job_title": "ML Engineer",
                    "company": "Acme",
                    "job_summary": "Fit WhatsApp role no email",
                    "poster_email": None,
                    "poster_number": "+15550001111",
                    "discard_reason": None,
                    "relevance_reason": "Strong relevance",
                }
            elif "fit whatsapp" in content:
                payload = {
                    "decision": "fit",
                    "decision_score": 1.0,
                    "relevant": True,
                    "score": 8,
                    "job_title": "Backend Engineer",
                    "company": "Acme",
                    "job_summary": "Fit WhatsApp role no email",
                    "poster_email": None,
                    "poster_number": "+15550003333",
                    "discard_reason": None,
                    "relevance_reason": "Strong relevance",
                }
            else:
                payload = {
                    "decision": "fit",
                    "decision_score": 1.0,
                    "relevant": True,
                    "score": 8,
                    "job_title": "ML Engineer",
                    "company": "Acme",
                    "job_summary": "Fit email recruiter@example.com",
                    "poster_email": "recruiter@example.com",
                    "poster_number": "+15550006666",
                    "discard_reason": None,
                    "relevance_reason": "Strong relevance",
                }
            return {"text": json.dumps(payload)}

        if "shortlisted_tracks" in content and "selected_resume_track" in content:
            payload = {
                "add_items": [
                    {
                        "section": "skills",
                        "action": "Highlight production Python experience",
                        "reason": "Python is repeated in the job summary",
                        "priority": 1,
                    }
                ],
                "remove_items": [],
                "keywords_to_inject": ["Python", "FastAPI", "LLM"],
                "sections_to_edit": ["summary", "skills", "experience_recent_role"],
                "ats_score_estimate_before": 55,
                "ats_score_estimate_after": 78,
                "research_reasoning": "Key stack alignment is strong after targeted updates.",
                "selected_resume_track": "resume_track_python_ml",
                "selected_resume_source_pdf": "data/resume-library/resume_track_python_ml.pdf",
                "selected_resume_match_reason": "Strongest Python and ML evidence density.",
                "experience_target_section": "experience_recent_role",
                "summary_focus": "Reframe the summary around Python, ML, and contract delivery impact.",
                "skills_gap_notes": ["Surface FastAPI and LLM orchestration terminology."],
                "hard_gaps": [],
                "edit_scope": ["summary", "skills", "experience_recent_role"],
            }
            return {"text": json.dumps(payload), "input_tokens": 30, "output_tokens": 40, "latency_ms": 5}

        if "quality-gate this resume iteration" in content:
            return {"text": json.dumps({"pass": True, "reason": "pass", "feedback": ""})}

        return {"text": "{}"}

    monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)

    backend_app = ingest_module.create_app(enable_polling=False)
    payloads = [
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000001",
            "message_text": "FIT EMAIL recruiter@example.com",
            "timestamp": 1710000001,
            "external_message_id": "system-fit-email",
        },
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000002",
            "message_text": "FIT WHATSAPP no email",
            "timestamp": 1710000002,
            "external_message_id": "system-fit-whatsapp",
        },
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000003",
            "message_text": "OKAYISH EMAIL reviewer@example.com",
            "timestamp": 1710000003,
            "external_message_id": "system-okayish-email",
        },
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000004",
            "message_text": "OKAYISH WHATSAPP no email",
            "timestamp": 1710000004,
            "external_message_id": "system-okayish-whatsapp",
        },
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000005",
            "message_text": "DISCARD MARKETING role",
            "timestamp": 1710000005,
            "external_message_id": "system-discard",
        },
        {
            "group_id": "GROUP1@g.us",
            "sender_number": "+15550000006",
            "message_text": "FIT WHATSAPP FAIL no email",
            "timestamp": 1710000006,
            "external_message_id": "system-fail",
        },
    ]

    try:
        with TestClient(backend_app) as client:
            for payload in payloads:
                response = client.post("/webhook/waha", json=payload)
                assert response.status_code == 200
                assert response.json()["status"] == "processed"

            overview_before = client.get("/api/ops/overview")
            assert overview_before.status_code == 200
            assert overview_before.json()["unprocessed_messages_count"] == 6
            assert overview_before.json()["sent_pipeline_count_24h"] == 0
            assert overview_before.json()["review_required_count"] == 0

            settings = get_settings()
            pipeline_runner = manager_module.ManagerPipelineRunner(
                session_factory=session_factory,
                settings=settings,
                agent_factory=_PolicyAwareRoutingFactory(settings=settings),
            )
            watcher = watcher_module.WatcherService(settings=settings, session_factory=session_factory)
            summary = await watcher.run_tick(pipeline_runner=pipeline_runner)

            assert summary["processed_count"] == 5
            assert summary["error_count"] == 1

            overview_after = client.get("/api/ops/overview")
            assert overview_after.status_code == 200
            overview_payload = overview_after.json()
            assert overview_payload["unprocessed_messages_count"] == 0
            assert overview_payload["sent_pipeline_count_24h"] == 2
            assert overview_payload["review_required_count"] == 2
            assert overview_payload["review_required_count_24h"] == 2
            assert overview_payload["discarded_pipeline_count_24h"] == 1
            assert overview_payload["failed_pipeline_count"] == 1
            assert overview_payload["failed_count_24h"] == 1

            review_queue = client.get("/api/ops/review-queue", params={"limit": 10})
            assert review_queue.status_code == 200
            review_rows = review_queue.json()
            assert len(review_rows) == 2
            assert {row["channel"] for row in review_rows} == {"email", "whatsapp"}
            assert all(row["status"] == "review_required" for row in review_rows)
            assert all(str(row["attachment_path"]).endswith(".docx") for row in review_rows)

            pipeline_runs = client.get("/api/ops/pipeline-runs", params={"limit": 20})
            assert pipeline_runs.status_code == 200
            pipeline_rows = pipeline_runs.json()
            assert len(pipeline_rows) == 6
            assert {row["status"] for row in pipeline_rows} == {
                "sent",
                "review_required",
                "discarded",
                "failed",
            }

            sync_engine = create_engine(sync_test_url)
            try:
                with sync_engine.connect() as conn:
                    processed_rows = conn.execute(
                        text(
                            """
                            SELECT message_text, processed, processing_error
                            FROM whatsapp_messages
                            ORDER BY source_timestamp ASC
                            """
                        )
                    ).all()
                    assert len(processed_rows) == 6
                    assert all(row[1] is True for row in processed_rows)
                    error_map = {row[0]: row[2] for row in processed_rows}
                    assert error_map["FIT WHATSAPP FAIL no email"]
                    assert "Outbound send failed" in error_map["FIT WHATSAPP FAIL no email"]
                    assert error_map["OKAYISH EMAIL reviewer@example.com"] is None
                    assert error_map["OKAYISH WHATSAPP no email"] is None

                    status_rows = conn.execute(
                        text(
                            """
                            SELECT wm.message_text, pr.status
                            FROM pipeline_runs pr
                            JOIN whatsapp_messages wm ON wm.id = pr.message_id
                            ORDER BY wm.source_timestamp ASC
                            """
                        )
                    ).all()
                    assert dict(status_rows) == {
                        "FIT EMAIL recruiter@example.com": "sent",
                        "FIT WHATSAPP no email": "sent",
                        "OKAYISH EMAIL reviewer@example.com": "review_required",
                        "OKAYISH WHATSAPP no email": "review_required",
                        "DISCARD MARKETING role": "discarded",
                        "FIT WHATSAPP FAIL no email": "failed",
                    }

            finally:
                sync_engine.dispose()

            second_tick = await watcher.run_tick(pipeline_runner=pipeline_runner)
            assert second_tick["processed_count"] == 0
            assert second_tick["error_count"] == 0
    finally:
        clear_mappers()
        await async_engine.dispose()
        clear_settings_cache()
        _run_alembic("downgrade")
