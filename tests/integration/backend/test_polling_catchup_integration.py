from __future__ import annotations

import asyncio
import importlib
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
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
        pytest.skip(f"PostgreSQL not available for polling integration tests: {exc}")
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
        "PDF_CONVERTER_MODEL": "claude-haiku-4-5-20251001",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": database_url,
        "TEST_DATABASE_URL": database_url,
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": "group-1@g.us",
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
        "OUTPUT_DIR": str(output_dir),
        "SKILLS_DIR": str(skills_dir),
        "LOG_LEVEL": "INFO",
        "LOG_FORMAT": "json",
        "OTEL_ENDPOINT": "http://localhost:4317",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


class _ReplayConnector:
    def __init__(self, messages_by_group: dict[str, list[dict[str, object]]]) -> None:
        self._messages_by_group = messages_by_group
        self.last_error: str | None = None
        self.calls: list[dict[str, object]] = []

    async def get_new_messages(
        self,
        group_id: str,
        since_timestamp: int,
        *,
        until_timestamp: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, object]]:
        self.calls.append(
            {
                "group_id": group_id,
                "since_timestamp": since_timestamp,
                "until_timestamp": until_timestamp,
                "limit": limit,
            }
        )
        messages = [
            message
            for message in self._messages_by_group.get(group_id, [])
            if int(message["timestamp"]) > since_timestamp
            and (until_timestamp is None or int(message["timestamp"]) <= until_timestamp)
        ]
        messages.sort(key=lambda item: (int(item["timestamp"]), str(item["id"])))
        return messages[:limit]

    async def close(self) -> None:
        return None


def test_polling_cursor_persists_across_restart_and_respects_cutoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async_test_url = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    sync_test_url = _async_to_sync_url(async_test_url)

    _ensure_database_exists(sync_test_url)
    _reset_public_schema(sync_test_url)
    _set_required_runtime_env(monkeypatch, tmp_path, async_test_url)
    clear_settings_cache()
    _run_alembic("upgrade")

    database_module = None
    try:
        clear_mappers()
        import job_platform.database as database_module
        import job_platform.models as models_module
        import job_backend.polling.waha_polling as polling_module
        import job_backend.services.ingest as ingest_module

        importlib.reload(database_module)
        importlib.reload(models_module)
        importlib.reload(polling_module)
        importlib.reload(ingest_module)

        reference_time = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
        first_cutoff = int((reference_time - timedelta(seconds=10)).timestamp())
        first_messages = {
            "group-1@g.us": [
                {
                    "id": "wa-1",
                    "text": "message-1",
                    "sender_number": "15550001111",
                    "timestamp": int((reference_time - timedelta(hours=1)).timestamp()),
                    "group_id": "group-1@g.us",
                },
                {
                    "id": "wa-2",
                    "text": "message-2",
                    "sender_number": "15550001111",
                    "timestamp": int((reference_time - timedelta(minutes=30)).timestamp()),
                    "group_id": "group-1@g.us",
                },
                {
                    "id": "wa-3",
                    "text": "message-3",
                    "sender_number": "15550001111",
                    "timestamp": int((reference_time - timedelta(minutes=1)).timestamp()),
                    "group_id": "group-1@g.us",
                },
                {
                    "id": "wa-4",
                    "text": "message-4",
                    "sender_number": "15550001111",
                    "timestamp": int((reference_time + timedelta(minutes=1)).timestamp()),
                    "group_id": "group-1@g.us",
                },
            ]
        }

        async def _run_polling_flow() -> None:
            ingest_service = ingest_module.IngestService(
                connector=_ReplayConnector(first_messages),
                enable_polling=False,
            )
            first_poller = polling_module.WahaPollingService(
                ingest_message=ingest_service.ingest_normalized_message,
                settings=ingest_service.settings,
                connector=_ReplayConnector(first_messages),
                now_provider=lambda: reference_time,
            )

            first_summary = await first_poller.run_once(cutoff_timestamp=first_cutoff)
            assert first_summary == {"processed_count": 3, "error_count": 0}

            engine = create_engine(sync_test_url)
            try:
                with engine.connect() as conn:
                    messages = conn.execute(
                        text(
                            """
                            SELECT external_message_id, source_timestamp, ingest_source
                            FROM whatsapp_messages
                            ORDER BY source_timestamp ASC
                            """
                        )
                    ).all()
                    assert [row[0] for row in messages] == ["wa-1", "wa-2", "wa-3"]
                    assert {row[2] for row in messages} == {"poll"}

                    cursor = conn.execute(
                        text(
                            """
                            SELECT last_successful_message_timestamp,
                                   last_cutoff_timestamp,
                                   status,
                                   last_error
                            FROM polling_cursors
                            WHERE group_id = 'group-1@g.us'
                            """
                        )
                    ).one()
                    assert cursor[0] == int((reference_time - timedelta(minutes=1)).timestamp())
                    assert cursor[1] == first_cutoff
                    assert cursor[2] == "ok"
                    assert cursor[3] is None
            finally:
                engine.dispose()

            restarted_time = reference_time + timedelta(minutes=30)
            second_cutoff = int(restarted_time.timestamp())
            second_messages = {
                "group-1@g.us": first_messages["group-1@g.us"]
                + [
                    {
                        "id": "wa-5",
                        "text": "message-5",
                        "sender_number": "15550001111",
                        "timestamp": int((reference_time + timedelta(minutes=20)).timestamp()),
                        "group_id": "group-1@g.us",
                    }
                ]
            }
            restarted_ingest = ingest_module.IngestService(
                connector=_ReplayConnector(second_messages),
                enable_polling=False,
            )
            second_poller = polling_module.WahaPollingService(
                ingest_message=restarted_ingest.ingest_normalized_message,
                settings=restarted_ingest.settings,
                connector=_ReplayConnector(second_messages),
                now_provider=lambda: restarted_time,
            )

            second_summary = await second_poller.run_once(cutoff_timestamp=second_cutoff)
            assert second_summary == {"processed_count": 2, "error_count": 0}

            engine = create_engine(sync_test_url)
            try:
                with engine.connect() as conn:
                    message_ids = conn.execute(
                        text(
                            """
                            SELECT external_message_id
                            FROM whatsapp_messages
                            ORDER BY source_timestamp ASC
                            """
                        )
                    ).scalars().all()
                    assert message_ids == ["wa-1", "wa-2", "wa-3", "wa-4", "wa-5"]

                    cursor = conn.execute(
                        text(
                            """
                            SELECT last_successful_message_timestamp,
                                   last_cutoff_timestamp,
                                   status
                            FROM polling_cursors
                            WHERE group_id = 'group-1@g.us'
                            """
                        )
                    ).one()
                    assert cursor[0] == int((reference_time + timedelta(minutes=20)).timestamp())
                    assert cursor[1] == second_cutoff
                    assert cursor[2] == "ok"
            finally:
                engine.dispose()

        asyncio.run(_run_polling_flow())
    finally:
        clear_mappers()
        if database_module is not None:
            asyncio.run(database_module.engine.dispose())
        clear_settings_cache()
        _run_alembic("downgrade")
