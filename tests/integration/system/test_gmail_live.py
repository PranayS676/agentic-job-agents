from __future__ import annotations

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

from job_agent_runtime.agents.gmail_agent import GmailAgent
from job_agent_runtime.orchestration.manager import ManagerAgent
from job_agent_runtime.agents.stub_agents import DefaultStubAgentFactory
from job_integrations.gmail import GmailConnector
from job_agent_runtime.agents.base_agent import BaseAgent
from job_platform.config import clear_settings_cache, get_settings
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
    except Exception as exc:  # pragma: no cover
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


def _has_valid_oauth_client(credentials_path: Path) -> bool:
    if not credentials_path.is_file():
        return False
    try:
        payload = json.loads(credentials_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    return isinstance(payload, dict) and ("installed" in payload or "web" in payload)


@pytest.mark.asyncio
async def test_gmail_live_send_and_outbox_persist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_settings_cache()
    settings = get_settings()
    connector = GmailConnector(settings=settings)
    if not _has_valid_oauth_client(Path(settings.gmail_credentials_path)):
        pytest.skip("Gmail OAuth credentials are not configured with installed/web client JSON.")
    if connector.token_status() != "valid":
        pytest.skip("Gmail token is missing or expired; run OAuth bootstrap first.")

    async_test_url = os.getenv("TEST_DATABASE_URL", settings.database_url)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)

    monkeypatch.setenv("DATABASE_URL", async_test_url)
    monkeypatch.setenv("TEST_DATABASE_URL", async_test_url)
    clear_settings_cache()
    settings = get_settings()

    _run_alembic("upgrade")
    upgraded = True

    async_engine = create_async_engine(async_test_url)
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    attachment_file = Path(settings.output_dir) / "resumes" / "gmail_live_test.docx"
    attachment_file.parent.mkdir(parents=True, exist_ok=True)
    attachment_file.write_text("live gmail docx test", encoding="utf-8")

    async def _fake_call_model(self, messages, trace_id, tools=None, max_tokens=2048):  # noqa: ANN001, ARG002
        return {"text": '{"subject":"Live Gmail Test","body":"This is an automated live Gmail integration test."}'}

    monkeypatch.setattr(BaseAgent, "_call_model", _fake_call_model)

    try:
        async with session_factory() as session:
            message_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO whatsapp_messages (group_id, sender_number, message_text, message_hash, processed)
                        VALUES ('GROUP1@g.us', '+15550000001', 'live gmail route', 'wm_step9_live_gmail', true)
                        RETURNING id
                        """
                    )
                )
            ).scalar_one()
            trace_id = (
                await session.execute(
                    text(
                        """
                        INSERT INTO pipeline_runs (
                            message_id,
                            status,
                            job_title,
                            company,
                            job_summary,
                            poster_email
                        )
                        VALUES (
                            :message_id,
                            'resume_ready',
                            'ML Engineer',
                            'Acme',
                            'Python and ML role',
                            :poster_email
                        )
                        RETURNING trace_id
                        """
                    ),
                    {"message_id": message_id, "poster_email": settings.sender_email},
                )
            ).scalar_one()

            tracer = AgentTracer(session)
            gmail_agent = GmailAgent(
                db_session=session,
                tracer=tracer,
                settings=settings,
            )
            outbound_result = await gmail_agent.run(
                context={
                    "job_title": "ML Engineer",
                    "company": "Acme",
                    "job_summary": "Python and ML role",
                    "poster_email": settings.sender_email,
                    "attachment_path": str(attachment_file),
                },
                trace_id=trace_id,
            )

            manager = ManagerAgent(
                db_session=session,
                tracer=tracer,
                settings=settings,
                agent_factory=DefaultStubAgentFactory(settings=settings),
            )
            await manager._persist_outbound_result(trace_id=trace_id, outbound_result=outbound_result)
            await session.commit()

            row = (
                await session.execute(
                    text(
                        """
                        SELECT channel, recipient, status, external_id, attachment_path
                        FROM outbox
                        WHERE trace_id = :trace_id
                        """
                    ),
                    {"trace_id": trace_id},
                )
            ).first()

            assert row is not None
            assert row[0] == "email"
            assert row[1] == settings.sender_email
            assert row[2] == "sent"
            assert row[3]
            assert row[4] == str(attachment_file)
    finally:
        await async_engine.dispose()
        clear_settings_cache()
        if upgraded:
            _run_alembic("downgrade")



