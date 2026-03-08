from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from job_platform.config import Settings, clear_settings_cache
from job_backend.services.ingest import IngestService, create_app


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    clear_settings_cache()
    yield
    clear_settings_cache()


class _NoopConnector:
    async def close(self) -> None:
        return None

    async def get_new_messages(
        self,
        group_id: str,
        since_timestamp: int,
        *,
        until_timestamp: int | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:  # noqa: ARG002
        return []


@dataclass
class _DummySettings:
    whatsapp_group_ids_list: list[str]
    poll_interval_seconds: int = 30


class _StubService:
    def __init__(self, status: str = "inserted", to_raise: Exception | None = None) -> None:
        self.settings = _DummySettings(whatsapp_group_ids_list=["group-1@g.us"])
        self.enable_polling = False
        self.last_poll_at: datetime | None = None
        self.last_webhook_at: datetime | None = None
        self.polling_service = SimpleNamespace(
            last_poll_started_at=None,
            last_poll_completed_at=None,
            polling_status="idle",
        )
        self.logger = SimpleNamespace(error=lambda *args, **kwargs: None)
        self._status = status
        self._to_raise = to_raise
        self.ingest_calls = 0
        self.start_called = False
        self.stop_called = False

    async def start(self) -> None:
        self.start_called = True

    async def stop(self) -> None:
        self.stop_called = True

    async def ingest_payload(self, payload: dict[str, Any], source: str) -> str:  # noqa: ARG002
        self.ingest_calls += 1
        if self._to_raise:
            raise self._to_raise
        return self._status


class _FakeColumn:
    def __eq__(self, other: object) -> tuple[str, object]:
        return ("eq", other)


class _FakeWhatsAppMessage:
    id = _FakeColumn()
    group_id = _FakeColumn()
    external_message_id = _FakeColumn()
    message_hash = _FakeColumn()

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _FakeSelect:
    def where(self, condition: object) -> "_FakeSelect":  # noqa: ARG002
        return self


def _fake_select(*args: object, **kwargs: object) -> _FakeSelect:  # noqa: ARG001
    return _FakeSelect()


class _FakeSession:
    def __init__(self, *, duplicate: bool = False, fail_commit: bool = False) -> None:
        self.duplicate = duplicate
        self.fail_commit = fail_commit
        self.added: list[_FakeWhatsAppMessage] = []
        self.rolled_back = False
        self.committed = False

    async def scalar(self, statement: object) -> object | None:  # noqa: ARG002
        return "exists" if self.duplicate else None

    def add(self, instance: _FakeWhatsAppMessage) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        if self.fail_commit:
            raise SQLAlchemyError("db unavailable")
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _SessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ARG002
        return False


def _patch_ingest_db(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    import job_platform.database as database_module
    import job_platform.models as models_module
    import job_backend.services.ingest as ingest_module

    monkeypatch.setattr(ingest_module, "select", _fake_select)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", lambda: _SessionContext(session))
    monkeypatch.setattr(models_module, "WhatsAppMessage", _FakeWhatsAppMessage)


def _set_required_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, group_ids: str) -> None:
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
        "PDF_CONVERTER_MODEL": "claude-haiku-4-5-20251001",
        "GMAIL_AGENT_MODEL": "claude-sonnet-4-6",
        "WHATSAPP_MSG_MODEL": "claude-sonnet-4-6",
        "DATABASE_URL": "postgresql+asyncpg://postgres:password@localhost:5432/jobagent",
        "WAHA_BASE_URL": "http://localhost:3000",
        "WAHA_SESSION": "default",
        "WAHA_API_KEY": "waha-test",
        "WHATSAPP_GROUP_IDS": group_ids,
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
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_webhook_returns_200_when_processed() -> None:
    service = _StubService(status="inserted")
    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            json={"chatId": "group-1@g.us", "from": "1111@c.us", "text": "hello"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "processed"}
        assert service.ingest_calls == 1


def test_webhook_duplicate_returns_200() -> None:
    service = _StubService(status="duplicate_ignored")
    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            json={"chatId": "group-1@g.us", "from": "1111@c.us", "text": "hello"},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "duplicate_ignored"}


def test_webhook_non_allowlisted_returns_202() -> None:
    service = _StubService(status="ignored_group")
    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            json={"chatId": "group-2@g.us", "from": "1111@c.us", "text": "hello"},
        )
        assert response.status_code == 202
        assert response.json() == {"status": "ignored_group"}


def test_webhook_malformed_payload_returns_202() -> None:
    service = _StubService(status="inserted")
    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            content="not-json",
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 202
        assert response.json() == {"status": "invalid_payload"}
        assert service.ingest_calls == 0


def test_webhook_db_failure_returns_503() -> None:
    service = _StubService(to_raise=SQLAlchemyError("db down"))
    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.post(
            "/webhook/waha",
            json={"chatId": "group-1@g.us", "from": "1111@c.us", "text": "hello"},
        )
        assert response.status_code == 503
        assert response.json() == {"status": "db_unavailable"}


def test_health_response_contract() -> None:
    service = _StubService(status="inserted")
    service.last_poll_at = datetime(2026, 1, 1, tzinfo=UTC)
    service.polling_service.last_poll_started_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    service.polling_service.last_poll_completed_at = datetime(2026, 1, 1, 12, 30, tzinfo=UTC)
    service.polling_service.polling_status = "ok"
    service.last_webhook_at = datetime(2026, 1, 2, tzinfo=UTC)

    app = create_app(enable_polling=False, service=service)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "ok"
        assert payload["groups_monitored"] == 1
        assert payload["polling_enabled"] is False
        assert payload["last_poll_started_at"].startswith("2026-01-01T12:00:00")
        assert payload["last_poll_completed_at"].startswith("2026-01-01T12:30:00")
        assert payload["polling_status"] == "ok"
        assert payload["last_poll_at"].startswith("2026-01-01")
        assert payload["last_webhook_at"].startswith("2026-01-02")


@pytest.mark.asyncio
async def test_ingest_payload_valid_message_inserts_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch, tmp_path, "group-1@g.us")
    session = _FakeSession(duplicate=False)
    _patch_ingest_db(monkeypatch, session)

    service = IngestService(
        settings=_DummySettings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=_NoopConnector(),
        enable_polling=False,
    )
    status = await service.ingest_payload(
        {
            "chatId": "group-1@g.us",
            "fromNumber": "15550001111",
            "text": "Python role available",
            "timestamp": 1234,
            "id": "wa-1",
        },
        source="webhook",
    )

    assert status == "inserted"
    assert session.committed is True
    assert len(session.added) == 1
    inserted = session.added[0]
    assert inserted.group_id == "group-1@g.us"
    assert inserted.sender_number == "15550001111"
    assert inserted.message_text == "Python role available"
    assert inserted.source_timestamp == 1234
    assert inserted.external_message_id == "wa-1"
    assert inserted.ingest_source == "webhook"
    expected_hash = hashlib.md5(
        "group-1@g.us|15550001111|Python role available|1234|wa-1".encode("utf-8")
    ).hexdigest()
    assert inserted.message_hash == expected_hash
    assert inserted.processed is False


@pytest.mark.asyncio
async def test_ingest_payload_duplicate_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch, tmp_path, "group-1@g.us")
    session = _FakeSession(duplicate=True)
    _patch_ingest_db(monkeypatch, session)

    service = IngestService(
        settings=_DummySettings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=_NoopConnector(),
        enable_polling=False,
    )
    status = await service.ingest_payload(
        {
            "chatId": "group-1@g.us",
            "fromNumber": "15550001111",
            "text": "duplicate",
        },
        source="webhook",
    )

    assert status == "duplicate_ignored"
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_ingest_payload_duplicate_external_message_id_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(monkeypatch, tmp_path, "group-1@g.us")
    session = _FakeSession(duplicate=True)
    _patch_ingest_db(monkeypatch, session)

    service = IngestService(
        settings=_DummySettings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=_NoopConnector(),
        enable_polling=False,
    )
    status = await service.ingest_payload(
        {
            "chatId": "group-1@g.us",
            "fromNumber": "15550001111",
            "text": "duplicate via external id",
            "timestamp": 2234,
            "id": "wa-duplicate-1",
        },
        source="webhook",
    )

    assert status == "duplicate_ignored"
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_ingest_payload_rejects_non_allowlisted_group() -> None:
    service = IngestService(
        settings=_DummySettings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=_NoopConnector(),
        enable_polling=False,
    )
    status = await service.ingest_payload(
        {
            "chatId": "other-group@g.us",
            "fromNumber": "15550001111",
            "text": "ignored",
        },
        source="webhook",
    )
    assert status == "ignored_group"


@pytest.mark.asyncio
async def test_ingest_payload_invalid_shape_returns_invalid_payload() -> None:
    service = IngestService(
        settings=_DummySettings(whatsapp_group_ids_list=["group-1@g.us"]),
        connector=_NoopConnector(),
        enable_polling=False,
    )
    status = await service.ingest_payload({"chatId": "group-1@g.us"}, source="webhook")
    assert status == "invalid_payload"


def test_allowlist_parser_handles_spaces_and_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_required_env(
        monkeypatch,
        tmp_path,
        "11111@g.us, 22222@g.us,11111@g.us,  ,22222@g.us",
    )
    settings = Settings()
    assert settings.whatsapp_group_ids_list == ["11111@g.us", "22222@g.us"]


