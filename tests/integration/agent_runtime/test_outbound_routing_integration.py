from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from job_agent_runtime.orchestration.manager import ManagerPipelineRunner
from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory, StubGmailAgent, StubWhatsAppMsgAgent
from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import clear_settings_cache, get_settings


ROOT_DIR = Path(__file__).resolve().parents[3]
DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5432/jobagent_step910_outbound_test"


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
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"PostgreSQL not available for integration tests: {exc}")
    finally:
        admin_engine.dispose()


def _reset_public_schema(sync_test_url: str) -> None:
    engine = create_engine(sync_test_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
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

    (data_dir / "base_resume.docx").write_text("placeholder", encoding="utf-8")
    (data_dir / "base_resume.md").write_text("placeholder", encoding="utf-8")
    (data_dir / "credentials.json").write_text("{}", encoding="utf-8")
    track_payload = {
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
        (resume_tracks_dir / f"resume_track_{suffix}.json").write_text(json.dumps(payload), encoding="utf-8")

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
        "GMAIL_CREDENTIALS_PATH": str(data_dir / "credentials.json"),
        "GMAIL_TOKEN_PATH": str(data_dir / "token.json"),
        "SENDER_EMAIL": "sender@example.com",
        "SENDER_NAME": "Pranay",
        "MIN_RELEVANCE_SCORE": "6",
        "MIN_ATS_SCORE": "65",
        "MAX_RESUME_EDIT_ITERATIONS": "2",
        "BASE_RESUME_DOCX": str(data_dir / "base_resume.docx"),
        "BASE_RESUME_TEXT": str(data_dir / "base_resume.md"),
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


class _FailingWhatsAppAgent(StubWhatsAppMsgAgent):
    async def run(self, context: dict, trace_id):  # noqa: ANN001
        _ = (context, trace_id)
        return {
            "sent": False,
            "channel": "whatsapp",
            "recipient": str(context["poster_number"]),
            "subject": None,
            "body_preview": "WAHA send failed",
            "attachment_path": context.get("attachment_path"),
            "external_id": None,
        }


class _RoutingFactory(DefaultStubAgentFactory):
    def __init__(self, settings) -> None:  # noqa: ANN001
        super().__init__(settings=settings)
        self._gmail = StubGmailAgent()
        self._whatsapp = _FailingWhatsAppAgent()


@pytest.mark.asyncio
async def test_outbound_routing_statuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async_test_url = DEFAULT_TEST_DATABASE_URL
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url)

    clear_settings_cache()
    _run_alembic("upgrade")
    upgraded = True

    async_engine = create_async_engine(async_test_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def _fake_call_model(self, messages, trace_id, tools=None, max_tokens=2048):  # noqa: ANN001, ARG002
        content = str(messages[-1].get("content", "")).lower()
        if "evaluate whether this whatsapp message" in content:
            with_email = "recruiter@example.com" in content
            payload = {
                "relevant": True,
                "score": 8,
                "job_title": "ML Engineer",
                "company": "Acme",
                "job_summary": "Python and ML role",
                "poster_email": "recruiter@example.com" if with_email else None,
                "poster_number": "+15550001111",
                "discard_reason": None,
                "relevance_reason": "Strong relevance",
            }
            return {"text": json.dumps(payload)}

        if "shortlisted_tracks" in content and "selected_resume_track" in content:
            payload = {
                "add_items": [{"section": "skills", "action": "x", "reason": "x", "priority": 1}],
                "remove_items": [],
                "keywords_to_inject": ["Python"],
                "sections_to_edit": ["summary", "skills", "experience_recent_role"],
                "ats_score_estimate_before": 55,
                "ats_score_estimate_after": 78,
                "research_reasoning": "ok",
                "selected_resume_track": "resume_track_python_ml",
                "selected_resume_source_pdf": "data/resume-library/resume_track_python_ml.pdf",
                "selected_resume_match_reason": "Strongest Python and ML evidence density.",
                "experience_target_section": "experience_recent_role",
                "summary_focus": "Align the summary to Python and ML delivery impact.",
                "skills_gap_notes": ["Surface grounded FastAPI and LLM keywords."],
                "hard_gaps": [],
                "edit_scope": ["summary", "skills", "experience_recent_role"],
            }
            return {"text": json.dumps(payload)}

        if "quality-gate this resume iteration" in content:
            return {"text": json.dumps({"pass": True, "reason": "pass", "feedback": ""})}

        return {"text": "{}"}

    monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)

    try:
        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (group_id, sender_number, message_text, message_hash)
                        VALUES
                          ('GROUP1@g.us', '+15550000001', 'Python ML role recruiter@example.com', 'wm_step9_hash_1'),
                          ('GROUP1@g.us', '+15550000002', 'Python ML role no email', 'wm_step9_hash_2')
                        """
                    )
                )
        finally:
            sync_engine.dispose()

        settings = get_settings()
        pipeline_runner = ManagerPipelineRunner(
            session_factory=session_factory,
            settings=settings,
            agent_factory=_RoutingFactory(settings=settings),
        )

        trace_rows = []
        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.begin() as conn:
                trace_rows = conn.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (message_id, status)
                        SELECT id, 'started'
                        FROM whatsapp_messages
                        ORDER BY created_at ASC
                        RETURNING trace_id, message_id
                        """
                    )
                ).all()
        finally:
            sync_engine.dispose()

        async with session_factory() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT id, group_id, sender_number, message_text
                        FROM whatsapp_messages
                        ORDER BY created_at ASC
                        """
                    )
                )
            ).mappings().all()

        first = rows[0]
        second = rows[1]
        await pipeline_runner.run(
            message=type(
                "Message",
                (),
                {
                    "id": first["id"],
                    "group_id": first["group_id"],
                    "sender_number": first["sender_number"],
                    "message_text": first["message_text"],
                },
            )(),
            trace_id=trace_rows[0][0],
        )
        with pytest.raises(RuntimeError, match="Outbound send failed"):
            await pipeline_runner.run(
                message=type(
                    "Message",
                    (),
                    {
                        "id": second["id"],
                        "group_id": second["group_id"],
                        "sender_number": second["sender_number"],
                        "message_text": second["message_text"],
                    },
                )(),
                trace_id=trace_rows[1][0],
            )

        sync_engine = create_engine(sync_test_url)
        try:
            with sync_engine.connect() as conn:
                outbox_rows = conn.execute(
                    text("SELECT channel, status FROM outbox ORDER BY sent_at ASC")
                ).all()
                assert len(outbox_rows) == 2
                assert outbox_rows[0][0] == "email"
                assert outbox_rows[0][1] == "sent"
                assert outbox_rows[1][0] == "whatsapp"
                assert outbox_rows[1][1] == "failed"

                statuses = conn.execute(
                    text("SELECT status FROM pipeline_runs ORDER BY created_at ASC")
                ).scalars().all()
                assert statuses == ["sent", "failed"]
        finally:
            sync_engine.dispose()
    finally:
        await async_engine.dispose()
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")



