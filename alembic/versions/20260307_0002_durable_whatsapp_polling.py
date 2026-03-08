"""Add durable WhatsApp polling cursor state and message metadata.

Revision ID: 20260307_0002
Revises: 20260305_0001
Create Date: 2026-03-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260307_0002"
down_revision = "20260305_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("whatsapp_messages", sa.Column("source_timestamp", sa.BigInteger(), nullable=True))
    op.add_column("whatsapp_messages", sa.Column("external_message_id", sa.Text(), nullable=True))
    op.add_column("whatsapp_messages", sa.Column("ingest_source", sa.Text(), nullable=True))
    op.create_index(
        "uq_whatsapp_group_external_message",
        "whatsapp_messages",
        ["group_id", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )

    op.create_table(
        "polling_cursors",
        sa.Column("group_id", sa.Text(), primary_key=True, nullable=False),
        sa.Column("last_successful_message_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("last_poll_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_poll_completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_cutoff_timestamp", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'bootstrapped'")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_polling_cursors_updated_at",
        "polling_cursors",
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_polling_cursors_updated_at", table_name="polling_cursors")
    op.drop_table("polling_cursors")

    op.drop_index("uq_whatsapp_group_external_message", table_name="whatsapp_messages")
    op.drop_column("whatsapp_messages", "ingest_source")
    op.drop_column("whatsapp_messages", "external_message_id")
    op.drop_column("whatsapp_messages", "source_timestamp")
