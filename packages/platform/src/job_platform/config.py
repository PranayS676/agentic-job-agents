from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    anthropic_api_key: SecretStr | None = None
    anthropic_auth_token: SecretStr | None = None
    anthropic_base_url: str | None = None

    manager_model: str | None = None
    research_model: str | None = None
    resume_editor_model: str | None = None
    gmail_agent_model: str | None = None
    whatsapp_msg_model: str | None = None

    database_url: str

    waha_base_url: str
    waha_session: str
    waha_api_key: str
    whatsapp_group_ids: str
    poll_interval_seconds: int

    gmail_credentials_path: Path | None = None
    gmail_token_path: Path | None = None
    sender_email: str | None = None
    sender_name: str | None = None

    min_relevance_score: int | None = None
    min_ats_score: int | None = None
    max_resume_edit_iterations: int | None = None

    base_resume_docx: Path | None = None
    base_resume_text: Path | None = None
    resume_library_dir: Path | None = None
    resume_tracks_dir: Path | None = None
    resume_docx_tracks_dir: Path | None = None
    output_dir: Path | None = None
    skills_dir: Path | None = None

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    log_format: Literal["json", "console"]
    otel_endpoint: str | None = None

    @field_validator("poll_interval_seconds")
    @classmethod
    def validate_poll_interval_seconds(cls, value: int) -> int:
        if value < 1:
            raise ValueError("poll_interval_seconds must be >= 1")
        return value

    @field_validator("anthropic_api_key", "anthropic_auth_token", mode="before")
    @classmethod
    def normalize_optional_secret(cls, value):
        if value is None:
            return None
        if isinstance(value, SecretStr):
            cleaned = value.get_secret_value().strip()
            return SecretStr(cleaned) if cleaned else None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("anthropic_base_url", mode="before")
    @classmethod
    def normalize_optional_string(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned or None

    @field_validator("min_relevance_score")
    @classmethod
    def validate_min_relevance_score(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value < 0 or value > 10:
            raise ValueError("min_relevance_score must be between 0 and 10")
        return value

    @field_validator("min_ats_score")
    @classmethod
    def validate_min_ats_score(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value < 0 or value > 100:
            raise ValueError("min_ats_score must be between 0 and 100")
        return value

    @field_validator("max_resume_edit_iterations")
    @classmethod
    def validate_max_resume_edit_iterations(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value < 1:
            raise ValueError("max_resume_edit_iterations must be >= 1")
        return value

    @field_validator("whatsapp_group_ids")
    @classmethod
    def validate_whatsapp_group_ids(cls, value: str) -> str:
        if not any(group.strip() for group in value.split(",")):
            raise ValueError("whatsapp_group_ids must contain at least one group id")
        return value

    @property
    def whatsapp_group_ids_list(self) -> list[str]:
        groups: list[str] = []
        seen: set[str] = set()
        for group in self.whatsapp_group_ids.split(","):
            cleaned = group.strip()
            if not cleaned:
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            groups.append(cleaned)
        if not groups:
            raise ValueError("whatsapp_group_ids_list is empty after parsing")
        return groups

    @property
    def db_url_sync(self) -> str:
        if self.database_url.startswith("postgresql+asyncpg://"):
            return self.database_url.replace(
                "postgresql+asyncpg://", "postgresql+psycopg2://", 1
            )
        if self.database_url.startswith("postgres+asyncpg://"):
            return self.database_url.replace("postgres+asyncpg://", "postgres+psycopg2://", 1)
        if self.database_url.startswith(
            (
                "postgresql+psycopg2://",
                "postgres+psycopg2://",
                "postgresql://",
                "postgres://",
            )
        ):
            return self.database_url
        raise ValueError(
            "database_url must use a PostgreSQL scheme "
            "(postgresql://, postgres://, or asyncpg variants)"
        )

    @property
    def project_root(self) -> Path:
        return PROJECT_ROOT

    def resolve_path(self, value: Path | str) -> Path:
        path = Path(value)
        if path.is_absolute():
            return path
        return (self.project_root / path).resolve()


def validate_backend_startup_requirements(settings: Settings) -> None:
    missing: list[str] = []

    if not settings.waha_base_url.strip():
        missing.append("Missing required setting: WAHA_BASE_URL")
    if not settings.waha_session.strip():
        missing.append("Missing required setting: WAHA_SESSION")

    if missing:
        details = "\n".join(f"- {entry}" for entry in missing)
        raise RuntimeError(f"Backend startup requirements validation failed:\n{details}")


def validate_agent_runtime_startup_requirements(settings: Settings) -> None:
    missing: list[str] = []

    if settings.anthropic_api_key is None and settings.anthropic_auth_token is None:
        missing.append(
            "Missing required setting: provide ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN"
        )
    for field_name in (
        "manager_model",
        "research_model",
        "resume_editor_model",
        "gmail_agent_model",
        "whatsapp_msg_model",
        "sender_email",
        "sender_name",
    ):
        value = getattr(settings, field_name)
        if not isinstance(value, str) or not value.strip():
            missing.append(f"Missing required setting: {field_name.upper()}")
    for field_name in ("min_relevance_score", "min_ats_score", "max_resume_edit_iterations"):
        if getattr(settings, field_name) is None:
            missing.append(f"Missing required setting: {field_name.upper()}")

    if settings.skills_dir is None:
        missing.append("Missing required setting: SKILLS_DIR")
    elif not settings.resolve_path(settings.skills_dir).is_dir():
        missing.append(f"Missing required directory: {settings.resolve_path(settings.skills_dir)}")

    if settings.output_dir is None:
        missing.append("Missing required setting: OUTPUT_DIR")
    elif not settings.resolve_path(settings.output_dir).is_dir():
        missing.append(f"Missing required directory: {settings.resolve_path(settings.output_dir)}")

    if settings.base_resume_docx is None:
        missing.append("Missing required setting: BASE_RESUME_DOCX")
    elif not settings.resolve_path(settings.base_resume_docx).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.base_resume_docx)}")

    if settings.base_resume_text is None:
        missing.append("Missing required setting: BASE_RESUME_TEXT")
    elif not settings.resolve_path(settings.base_resume_text).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.base_resume_text)}")

    if settings.resume_library_dir is None:
        missing.append("Missing required setting: RESUME_LIBRARY_DIR")
    elif not settings.resolve_path(settings.resume_library_dir).is_dir():
        missing.append(f"Missing required directory: {settings.resolve_path(settings.resume_library_dir)}")

    if settings.resume_tracks_dir is None:
        missing.append("Missing required setting: RESUME_TRACKS_DIR")
    else:
        resume_tracks_dir = settings.resolve_path(settings.resume_tracks_dir)
        if not resume_tracks_dir.is_dir():
            missing.append(f"Missing required directory: {resume_tracks_dir}")
        elif len(list(resume_tracks_dir.glob('*.json'))) < 3:
            missing.append(
                f"Missing required resume track files: expected at least 3 JSON tracks in {resume_tracks_dir}"
            )

    if settings.resume_docx_tracks_dir is None:
        missing.append("Missing required setting: RESUME_DOCX_TRACKS_DIR")
    else:
        resume_docx_tracks_dir = settings.resolve_path(settings.resume_docx_tracks_dir)
        if not resume_docx_tracks_dir.is_dir():
            missing.append(f"Missing required directory: {resume_docx_tracks_dir}")
        elif len(list(resume_docx_tracks_dir.glob("*.docx"))) < 3:
            missing.append(
                "Missing required resume DOCX track files: "
                f"expected at least 3 DOCX tracks in {resume_docx_tracks_dir}"
            )

    if settings.gmail_credentials_path is None:
        missing.append("Missing required setting: GMAIL_CREDENTIALS_PATH")
    elif not settings.resolve_path(settings.gmail_credentials_path).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.gmail_credentials_path)}")
    if missing:
        details = "\n".join(f"- {entry}" for entry in missing)
        raise RuntimeError(f"Agent runtime startup requirements validation failed:\n{details}")


def validate_startup_requirements(settings: Settings) -> None:
    missing: list[str] = []

    if settings.skills_dir is None:
        missing.append("Missing required setting: SKILLS_DIR")
    elif not settings.resolve_path(settings.skills_dir).is_dir():
        missing.append(f"Missing required directory: {settings.resolve_path(settings.skills_dir)}")

    if settings.output_dir is None:
        missing.append("Missing required setting: OUTPUT_DIR")
    elif not settings.resolve_path(settings.output_dir).is_dir():
        missing.append(f"Missing required directory: {settings.resolve_path(settings.output_dir)}")

    if settings.base_resume_docx is None:
        missing.append("Missing required setting: BASE_RESUME_DOCX")
    elif not settings.resolve_path(settings.base_resume_docx).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.base_resume_docx)}")

    if settings.base_resume_text is None:
        missing.append("Missing required setting: BASE_RESUME_TEXT")
    elif not settings.resolve_path(settings.base_resume_text).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.base_resume_text)}")

    if settings.gmail_credentials_path is None:
        missing.append("Missing required setting: GMAIL_CREDENTIALS_PATH")
    elif not settings.resolve_path(settings.gmail_credentials_path).is_file():
        missing.append(f"Missing required file: {settings.resolve_path(settings.gmail_credentials_path)}")

    if missing:
        details = "\n".join(f"- {entry}" for entry in missing)
        raise RuntimeError(f"Startup requirements validation failed:\n{details}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def clear_settings_cache() -> None:
    get_settings.cache_clear()
