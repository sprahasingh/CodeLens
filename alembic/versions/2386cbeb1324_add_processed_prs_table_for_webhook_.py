"""add processed_prs table for webhook idempotency

Revision ID: 2386cbeb1324
Revises: a138ff00aa60
Create Date: 2026-09-26 04:04:42.016063

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2386cbeb1324'
down_revision: Union[str, Sequence[str], None] = 'a138ff00aa60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'processed_prs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('repo_owner', sa.String(255), nullable=False),
        sa.Column('repo_name', sa.String(255), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('head_sha', sa.String(64), nullable=False),
        sa.Column('processed_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            'repo_owner', 'repo_name', 'pr_number', 'head_sha',
            name='uq_processed_prs_repo_pr_sha'
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('processed_prs')
