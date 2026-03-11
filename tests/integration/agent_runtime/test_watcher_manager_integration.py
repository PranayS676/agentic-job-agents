from __future__ import annotations

import importlib
import json
import os
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import clear_mappers

from job_agent_runtime.agents.base_agent import BaseAgent
from job_agent_runtime.agents.stub_agents import (
    DefaultStubAgentFactory,
    StubGmailAgent,
    StubWhatsAppMsgAgent,
)
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
        self._gmail = StubGmailAgent()
        self._whatsapp = _PolicyAwareWhatsAppAgent()


@pytest.mark.asyncio
async def test_watcher_manager_terminal_states_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url)

    clear_settings_cache()
    _run_alembic("upgrade")
    upgraded = True

    clear_mappers()
    import job_agent_runtime.orchestration.manager as manager_module
    import job_platform.database as database_module
    import job_platform.models as models_module
    import job_agent_runtime.worker.watcher as watcher_module

    importlib.reload(database_module)
    importlib.reload(models_module)
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
            elif "fit email retry" in content:
                payload = {
                    "decision": "fit",
                    "decision_score": 1.0,
                    "relevant": True,
                    "score": 8,
                    "job_title": "ML Engineer",
                    "company": "Acme",
                    "job_summary": "Fit email retry recruiter_retry@example.com",
                    "poster_email": "recruiter_retry@example.com",
                    "poster_number": "+15550005555",
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
                "remove_items": [
                    {
                        "section": "skills",
                        "action": "Trim unrelated legacy bullets",
                        "reason": "Improve relevance density",
                    }
                ],
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

        return {"text": "{}"}

    async def _fake_quality_gate(self, trace_id, resume_output, attempt):  # noqa: ANN001
        row = (
            await self.db_session.execute(
                text(
                    """
                    SELECT wm.message_text
                    FROM pipeline_runs pr
                    JOIN whatsapp_messages wm ON wm.id = pr.message_id
                    WHERE pr.trace_id = :trace_id
                    """
                ),
                {"trace_id": trace_id},
            )
        ).scalar_one()
        message_text = str(row).lower()
        if "fit email retry" in message_text and attempt == 0:
            return {
                "passed": False,
                "model_pass": False,
                "criteria_pass": False,
                "feedback": "Need stronger impact bullets",
                "reason": "first pass failed",
                "evaluator_passed": False,
                "ats_score_after": int(resume_output["ats_score_after"]),
            }
        return {
            "passed": True,
            "model_pass": True,
            "criteria_pass": True,
            "feedback": "",
            "reason": "pass",
            "evaluator_passed": True,
            "ats_score_after": int(resume_output["ats_score_after"]),
        }

    monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)
    monkeypatch.setattr(manager_module.ManagerAgent, "_run_quality_gate", _fake_quality_gate)

    try:
        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (group_id, sender_number, message_text, message_hash)
                        VALUES
                          ('GROUP1@g.us', '+15550000001', 'FIT EMAIL recruiter@example.com', 'wm_step7_hash_1'),
                          ('GROUP1@g.us', '+15550000002', 'FIT WHATSAPP no email', 'wm_step7_hash_2'),
                          ('GROUP1@g.us', '+15550000003', 'OKAYISH EMAIL reviewer@example.com', 'wm_step7_hash_3'),
                          ('GROUP1@g.us', '+15550000004', 'OKAYISH WHATSAPP no email', 'wm_step7_hash_4'),
                          ('GROUP1@g.us', '+15550000005', 'DISCARD MARKETING role', 'wm_step7_hash_5'),
                          ('GROUP1@g.us', '+15550000006', 'FIT EMAIL RETRY recruiter_retry@example.com', 'wm_step7_hash_6'),
                          ('GROUP1@g.us', '+15550000007', 'FIT WHATSAPP FAIL no email', 'wm_step7_hash_7')
                        """
                    )
                )
        finally:
            sync_engine.dispose()

        settings = get_settings()
        pipeline_runner = manager_module.ManagerPipelineRunner(
            session_factory=session_factory,
            settings=settings,
            agent_factory=_PolicyAwareRoutingFactory(settings=settings),
        )
        watcher = watcher_module.WatcherService(settings=settings, session_factory=session_factory)

        summary = await watcher.run_tick(pipeline_runner=pipeline_runner)
        assert summary["processed_count"] == 6
        assert summary["error_count"] == 1

        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.connect() as conn:
                processed_rows = conn.execute(
                    text(
                        """
                        SELECT message_text, processed, processing_error
                        FROM whatsapp_messages
                        ORDER BY created_at ASC
                        """
                    )
                ).all()
                assert len(processed_rows) == 7
                assert all(row[1] is True for row in processed_rows)

                processing_errors = {row[0]: row[2] for row in processed_rows}
                assert processing_errors["FIT WHATSAPP FAIL no email"]
                assert "Outbound send failed" in processing_errors["FIT WHATSAPP FAIL no email"]
                assert processing_errors["OKAYISH EMAIL reviewer@example.com"] is None
                assert processing_errors["OKAYISH WHATSAPP no email"] is None
                assert processing_errors["DISCARD MARKETING role"] is None

                pipeline_rows = conn.execute(
                    text(
                        """
                        SELECT wm.message_text, pr.status, pr.error_stage, pr.manager_decision, pr.research_output
                        FROM pipeline_runs pr
                        JOIN whatsapp_messages wm ON wm.id = pr.message_id
                        ORDER BY wm.created_at ASC
                        """
                    )
                ).all()
                assert len(pipeline_rows) == 7

                expected_statuses = {
                    "FIT EMAIL recruiter@example.com": "sent",
                    "FIT WHATSAPP no email": "sent",
                    "OKAYISH EMAIL reviewer@example.com": "review_required",
                    "OKAYISH WHATSAPP no email": "review_required",
                    "DISCARD MARKETING role": "discarded",
                    "FIT EMAIL RETRY recruiter_retry@example.com": "sent",
                    "FIT WHATSAPP FAIL no email": "failed",
                }
                expected_error_stages = {
                    "FIT WHATSAPP FAIL no email": "routing",
                }
                expected_sequences = {
                    "FIT EMAIL recruiter@example.com": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "sent",
                    ],
                    "FIT WHATSAPP no email": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "sent",
                    ],
                    "OKAYISH EMAIL reviewer@example.com": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "review_required",
                    ],
                    "OKAYISH WHATSAPP no email": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "review_required",
                    ],
                    "DISCARD MARKETING role": [
                        "relevance_done",
                        "discarded",
                    ],
                    "FIT EMAIL RETRY recruiter_retry@example.com": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "quality_retry",
                        "resume_ready",
                        "sent",
                    ],
                    "FIT WHATSAPP FAIL no email": [
                        "relevance_done",
                        "research_done",
                        "resume_ready",
                        "failed",
                    ],
                }

                for message_text, status, error_stage, manager_decision, research_output in pipeline_rows:
                    assert status == expected_statuses[message_text]
                    assert error_stage == expected_error_stages.get(message_text)
                    events = list((manager_decision or {}).get("events") or [])
                    assert [event["status"] for event in events] == expected_sequences[message_text]
                    if status == "discarded":
                        assert research_output is None
                    else:
                        assert isinstance(research_output, dict)
                        assert research_output["selected_resume_track"] == "resume_track_python_ml"
                        assert research_output["experience_target_section"] == "experience_recent_role"

                resume_rows = conn.execute(
                    text(
                        """
                        SELECT wm.message_text, rv.version_number, rv.attachment_path
                        FROM resume_versions rv
                        JOIN pipeline_runs pr ON pr.trace_id = rv.trace_id
                        JOIN whatsapp_messages wm ON wm.id = pr.message_id
                        ORDER BY wm.created_at ASC, rv.version_number ASC
                        """
                    )
                ).all()
                assert len(resume_rows) == 7
                retry_versions = [
                    row[1] for row in resume_rows if row[0] == "FIT EMAIL RETRY recruiter_retry@example.com"
                ]
                assert retry_versions == [1, 2]
                assert all(row[2] for row in resume_rows)

                outbox_rows = conn.execute(
                    text(
                        """
                        SELECT wm.message_text, o.channel, o.status, o.attachment_path
                        FROM outbox o
                        JOIN pipeline_runs pr ON pr.trace_id = o.trace_id
                        JOIN whatsapp_messages wm ON wm.id = pr.message_id
                        ORDER BY wm.created_at ASC, o.sent_at ASC
                        """
                    )
                ).all()
                assert len(outbox_rows) == 6
                expected_outbox = {
                    "FIT EMAIL recruiter@example.com": ("email", "sent"),
                    "FIT WHATSAPP no email": ("whatsapp", "sent"),
                    "OKAYISH EMAIL reviewer@example.com": ("email", "review_required"),
                    "OKAYISH WHATSAPP no email": ("whatsapp", "review_required"),
                    "FIT EMAIL RETRY recruiter_retry@example.com": ("email", "sent"),
                    "FIT WHATSAPP FAIL no email": ("whatsapp", "failed"),
                }
                for message_text, channel, outbox_status, attachment_path in outbox_rows:
                    assert (channel, outbox_status) == expected_outbox[message_text]
                    assert attachment_path

                conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (
                            group_id, sender_number, message_text, message_hash, processed, created_at
                        )
                        VALUES (
                            'GROUP1@g.us',
                            '+15550000099',
                            'old message',
                            'wm_step7_old_hash',
                            true,
                            NOW() - INTERVAL '31 days'
                        )
                        """
                    )
                )
                conn.commit()
        finally:
            sync_engine.dispose()

        deleted = await watcher.run_cleanup_once()
        assert deleted >= 1

        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.connect() as conn:
                remaining = conn.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM whatsapp_messages
                        WHERE message_hash = 'wm_step7_old_hash'
                        """
                    )
                ).scalar_one()
                assert remaining == 0
        finally:
            sync_engine.dispose()
    finally:
        clear_mappers()
        await async_engine.dispose()
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")
