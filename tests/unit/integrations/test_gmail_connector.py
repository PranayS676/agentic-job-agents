from __future__ import annotations

from pathlib import Path

import pytest

from job_integrations.gmail import GmailConnector


class _DummyCreds:
    def __init__(self, *, valid: bool, expired: bool = False) -> None:
        self.valid = valid
        self.expired = expired
        self.refresh_token = "rtok"

    def to_json(self) -> str:
        return '{"token":"abc"}'


def _minimal_settings(tmp_path: Path):
    credentials = tmp_path / "credentials.json"
    token = tmp_path / "token.json"
    credentials.write_text("{}", encoding="utf-8")
    return type(
        "Settings",
        (),
        {
            "gmail_credentials_path": credentials,
            "gmail_token_path": token,
            "sender_email": "sender@example.com",
        },
    )()


def test_token_status_missing(tmp_path: Path) -> None:
    connector = GmailConnector(settings=_minimal_settings(tmp_path))
    assert connector.token_status() == "missing"


def test_token_status_valid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _minimal_settings(tmp_path)
    settings.gmail_token_path.write_text("{}", encoding="utf-8")

    import job_integrations.gmail as gmail_module

    monkeypatch.setattr(
        gmail_module.Credentials,
        "from_authorized_user_file",
        lambda *_args, **_kwargs: _DummyCreds(valid=True),
    )

    connector = GmailConnector(settings=settings)
    assert connector.token_status() == "valid"


@pytest.mark.asyncio
async def test_send_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _minimal_settings(tmp_path)
    attachment = tmp_path / "resume.pdf"
    attachment.write_bytes(b"%PDF-1.4\n")
    connector = GmailConnector(settings=settings)

    import job_integrations.gmail as gmail_module

    monkeypatch.setattr(connector, "_load_or_refresh_credentials", lambda: _DummyCreds(valid=True))

    class _FakeSender:
        def execute(self):
            return {"id": "gmail-msg-123"}

    class _FakeMessages:
        def send(self, userId, body):  # noqa: ANN001, N803
            assert userId == "me"
            assert "raw" in body
            return _FakeSender()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    monkeypatch.setattr(gmail_module, "build", lambda *args, **kwargs: _FakeService())

    message_id = await connector.send(
        to="target@example.com",
        subject="Hello",
        body="World",
        attachment_path=str(attachment),
    )
    assert message_id == "gmail-msg-123"


@pytest.mark.asyncio
async def test_send_fails_for_missing_attachment(tmp_path: Path) -> None:
    connector = GmailConnector(settings=_minimal_settings(tmp_path))
    with pytest.raises(RuntimeError, match="Attachment not found"):
        await connector.send(
            to="target@example.com",
            subject="x",
            body="y",
            attachment_path=str(tmp_path / "missing.pdf"),
        )


