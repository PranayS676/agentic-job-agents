"""Switch resume delivery artifact from pdf_path to attachment_path.

Revision ID: 20260310_0003
Revises: 20260307_0002
Create Date: 2026-03-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "20260310_0003"
down_revision = "20260307_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resume_versions", sa.Column("attachment_path", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE resume_versions
        SET attachment_path = COALESCE(pdf_path, docx_path)
        """
    )
    op.drop_column("resume_versions", "pdf_path")


def downgrade() -> None:
    op.add_column("resume_versions", sa.Column("pdf_path", sa.Text(), nullable=True))
    op.execute(
        """
        UPDATE resume_versions
        SET pdf_path = attachment_path
        """
    )
    op.drop_column("resume_versions", "attachment_path")
