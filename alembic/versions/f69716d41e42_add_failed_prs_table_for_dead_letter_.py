"""add failed_prs table for dead-letter handling

Revision ID: f69716d41e42
Revises: e871bb2b6bca
Create Date: 2026-09-26 23:18:31.381774

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f69716d41e42'
down_revision: Union[str, Sequence[str], None] = 'e871bb2b6bca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'failed_prs',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('repo_owner', sa.String(255), nullable=False),
        sa.Column('repo_name', sa.String(255), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('failed_at', sa.DateTime(), nullable=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('failed_prs')
