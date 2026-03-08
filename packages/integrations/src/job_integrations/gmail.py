from __future__ import annotations

import asyncio
import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from job_platform.config import Settings, get_settings


class GmailConnector:
    SCOPES = [
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
    ]

    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.gmail_credentials_path is None or self.settings.gmail_token_path is None:
            raise RuntimeError("Gmail credentials paths are not configured")
        if not self.settings.sender_email:
            raise RuntimeError("SENDER_EMAIL is not configured")
        self.credentials_path = self._resolve_path(self.settings.gmail_credentials_path)
        self.token_path = self._resolve_path(self.settings.gmail_token_path)
        self.sender_email = self.settings.sender_email

    def token_status(self) -> str:
        if not self.token_path.is_file():
            return "missing"

        try:
            creds = Credentials.from_authorized_user_file(
                str(self.token_path),
                self.SCOPES,
            )
        except Exception:
            return "missing"

        if creds.valid:
            return "valid"
        if creds.expired:
            return "expired"
        return "missing"

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_path: str | None = None,
    ) -> str:
        return await asyncio.to_thread(
            self._send_sync,
            to,
            subject,
            body,
            attachment_path,
        )

    def _send_sync(
        self,
        to: str,
        subject: str,
        body: str,
        attachment_path: str | None = None,
    ) -> str:
        if not self.credentials_path.is_file():
            raise RuntimeError(
                "Gmail credentials file not found at "
                f"{self.credentials_path}. Download OAuth credentials and update GMAIL_CREDENTIALS_PATH."
            )

        raw_message = self._build_raw_message(
            to=to,
            subject=subject,
            body=body,
            attachment_path=attachment_path,
        )

        creds = self._load_or_refresh_credentials()

        try:
            service = build(
                "gmail",
                "v1",
                credentials=creds,
                cache_discovery=False,
            )
            response = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": raw_message})
                .execute()
            )
        except HttpError as exc:
            raise RuntimeError(f"Gmail API send failed: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Gmail send failed: {exc}") from exc

        message_id = str(response.get("id") or "").strip()
        if not message_id:
            raise RuntimeError("Gmail send succeeded but no message id was returned.")
        return message_id

    def _load_or_refresh_credentials(self) -> Credentials:
        creds: Credentials | None = None
        if self.token_path.is_file():
            creds = Credentials.from_authorized_user_file(
                str(self.token_path),
                self.SCOPES,
            )

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._persist_token(creds)
            return creds

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            self.SCOPES,
        )
        creds = flow.run_local_server(port=0)
        self._persist_token(creds)
        return creds

    def _persist_token(self, creds: Credentials) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")

    def _build_raw_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachment_path: str | None,
    ) -> str:
        message = MIMEMultipart()
        message["To"] = to
        message["From"] = self.sender_email
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))

        if attachment_path:
            file_path = Path(attachment_path)
            if not file_path.is_file():
                raise RuntimeError(f"Attachment not found: {file_path}")
            part = MIMEApplication(file_path.read_bytes(), Name=file_path.name)
            part["Content-Disposition"] = f'attachment; filename="{file_path.name}"'
            message.attach(part)

        return base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")

    def _resolve_path(self, value: Path | str) -> Path:
        resolver = getattr(self.settings, "resolve_path", None)
        if callable(resolver):
            return resolver(value)
        return Path(value)

