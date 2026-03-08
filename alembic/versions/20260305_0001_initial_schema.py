"""Initial PostgreSQL schema for WhatsApp job agent.

Revision ID: 20260305_0001
Revises:
Create Date: 2026-03-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "20260305_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "whatsapp_messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("group_id", sa.Text(), nullable=False),
        sa.Column("sender_number", sa.Text(), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("message_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("processed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("processing_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("processing_error", sa.Text(), nullable=True),
    )
    op.create_index(
        "idx_whatsapp_unprocessed",
        "whatsapp_messages",
        ["processed", "created_at"],
        unique=False,
    )

    op.create_table(
        "pipeline_runs",
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("whatsapp_messages.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("job_title", sa.Text(), nullable=True),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("job_summary", sa.Text(), nullable=True),
        sa.Column("poster_number", sa.Text(), nullable=True),
        sa.Column("poster_email", sa.Text(), nullable=True),
        sa.Column("relevance_score", sa.Integer(), nullable=True),
        sa.Column("relevance_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'started'")),
        sa.Column("manager_decision", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("research_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("resume_eval", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("quality_gate_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outbound_action", sa.Text(), nullable=True),
        sa.Column("error_stage", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("idx_pipeline_status", "pipeline_runs", ["status", "created_at"], unique=False)

    op.create_table(
        "resume_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.trace_id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("docx_path", sa.Text(), nullable=True),
        sa.Column("pdf_path", sa.Text(), nullable=True),
        sa.Column("changes_made", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ats_score_before", sa.Integer(), nullable=True),
        sa.Column("ats_score_after", sa.Integer(), nullable=True),
        sa.Column("evaluator_passed", sa.Boolean(), nullable=True),
    )

    op.create_table(
        "outbox",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.trace_id"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body_preview", sa.Text(), nullable=True),
        sa.Column("attachment_path", sa.Text(), nullable=True),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'sent'")),
    )

    op.create_table(
        "candidate_profile",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.Text(), nullable=True),
        sa.Column("resume_text", sa.Text(), nullable=False),
        sa.Column("target_roles", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("target_stack", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("location_pref", sa.Text(), nullable=True),
    )

    op.create_table(
        "agent_traces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "trace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pipeline_runs.trace_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("full_input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("full_output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("idx_traces_trace_id", "agent_traces", ["trace_id", "created_at"], unique=False)

    op.execute(
        """
        CREATE OR REPLACE FUNCTION set_pipeline_runs_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_pipeline_runs_updated_at
        BEFORE UPDATE ON pipeline_runs
        FOR EACH ROW
        EXECUTE FUNCTION set_pipeline_runs_updated_at();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_pipeline_runs_updated_at ON pipeline_runs")
    op.execute("DROP FUNCTION IF EXISTS set_pipeline_runs_updated_at")

    op.drop_index("idx_traces_trace_id", table_name="agent_traces")
    op.drop_table("agent_traces")

    op.drop_table("candidate_profile")
    op.drop_table("outbox")
    op.drop_table("resume_versions")

    op.drop_index("idx_pipeline_status", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")

    op.drop_index("idx_whatsapp_unprocessed", table_name="whatsapp_messages")
    op.drop_table("whatsapp_messages")
