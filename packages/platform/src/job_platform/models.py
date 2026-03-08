from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class WhatsAppMessage(Base):
    __tablename__ = "whatsapp_messages"
    __table_args__ = (
        Index("idx_whatsapp_unprocessed", "processed", "created_at"),
        Index(
            "uq_whatsapp_group_external_message",
            "group_id",
            "external_message_id",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    sender_number: Mapped[str] = mapped_column(Text, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    external_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ingest_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    processing_started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    pipeline_runs: Mapped[list["PipelineRun"]] = relationship(back_populates="message")


class PollingCursor(Base):
    __tablename__ = "polling_cursors"
    __table_args__ = (
        Index("idx_polling_cursors_updated_at", "updated_at"),
    )

    group_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_successful_message_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_poll_started_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_poll_completed_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cutoff_timestamp: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'bootstrapped'"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("idx_pipeline_status", "status", "created_at"),
    )

    trace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("whatsapp_messages.id"),
        nullable=False,
    )
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    job_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    company: Mapped[str | None] = mapped_column(Text, nullable=True)
    job_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_number: Mapped[str | None] = mapped_column(Text, nullable=True)
    poster_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    relevance_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relevance_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("'started'"),
    )

    manager_decision: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    research_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    resume_eval: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    quality_gate_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    outbound_action: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    message: Mapped[WhatsAppMessage] = relationship(back_populates="pipeline_runs")
    resume_versions: Mapped[list["ResumeVersion"]] = relationship(back_populates="pipeline_run")
    outbox_messages: Mapped[list["Outbox"]] = relationship(back_populates="pipeline_run")
    traces: Mapped[list["AgentTrace"]] = relationship(back_populates="pipeline_run")


class ResumeVersion(Base):
    __tablename__ = "resume_versions"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    trace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pipeline_runs.trace_id"),
        nullable=False,
    )
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    docx_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    changes_made: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ats_score_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ats_score_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluator_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="resume_versions")


class Outbox(Base):
    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    trace_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pipeline_runs.trace_id"),
        nullable=False,
    )
    sent_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    recipient: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'sent'"))

    pipeline_run: Mapped[PipelineRun] = relationship(back_populates="outbox_messages")


class CandidateProfile(Base):
    __tablename__ = "candidate_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    phone: Mapped[str | None] = mapped_column(Text, nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_text: Mapped[str] = mapped_column(Text, nullable=False)
    target_roles: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    target_stack: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    location_pref: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentTrace(Base):
    __tablename__ = "agent_traces"
    __table_args__ = (
        Index("idx_traces_trace_id", "trace_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    trace_id: Mapped[UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pipeline_runs.trace_id"),
        nullable=True,
    )
    created_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    full_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    pipeline_run: Mapped[PipelineRun | None] = relationship(back_populates="traces")
