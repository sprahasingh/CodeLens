"""add durable review claim state, lease and result checkpoint

Revision ID: b7d09a4f6c21
Revises: f69716d41e42
Create Date: 2026-10-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7d09a4f6c21"
down_revision: Union[str, Sequence[str], None] = "f69716d41e42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("processed_prs", sa.Column("status", sa.String(length=32), nullable=True))
    op.add_column("processed_prs", sa.Column("task_id", sa.String(length=255), nullable=True))
    op.add_column("processed_prs", sa.Column("lease_owner", sa.String(length=255), nullable=True))
    op.add_column("processed_prs", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("processed_prs", sa.Column("attempts", sa.Integer(), nullable=True))
    op.add_column("processed_prs", sa.Column("dispatch_attempts", sa.Integer(), nullable=True))
    op.add_column("processed_prs", sa.Column("review_payload", sa.JSON(), nullable=True))
    op.add_column("processed_prs", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("processed_prs", sa.Column("dispatched_at", sa.DateTime(), nullable=True))

    # Rows created before this migration represent previously claimed reviews.
    # Keep them complete so deploy cannot replay historical PR heads.
    op.execute("UPDATE processed_prs SET status = 'completed', attempts = 0, dispatch_attempts = 0")
    op.alter_column("processed_prs", "status", nullable=False, server_default="queued")
    op.alter_column("processed_prs", "attempts", nullable=False, server_default="0")
    op.alter_column("processed_prs", "dispatch_attempts", nullable=False, server_default="0")
    op.create_index(
        "ix_processed_prs_status_lease",
        "processed_prs",
        ["status", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_processed_prs_status_lease", table_name="processed_prs")
    op.drop_column("processed_prs", "dispatched_at")
    op.drop_column("processed_prs", "last_error")
    op.drop_column("processed_prs", "review_payload")
    op.drop_column("processed_prs", "attempts")
    op.drop_column("processed_prs", "dispatch_attempts")
    op.drop_column("processed_prs", "lease_expires_at")
    op.drop_column("processed_prs", "lease_owner")
    op.drop_column("processed_prs", "task_id")
    op.drop_column("processed_prs", "status")
