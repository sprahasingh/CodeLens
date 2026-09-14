"""add predictions table for evaluation loop

Revision ID: a1f45e9be81a
Revises: 822f8635a9f2
Create Date: 2026-09-14 15:18:23.469793

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f45e9be81a'
down_revision: Union[str, Sequence[str], None] = '822f8635a9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'predictions',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('repo_owner', sa.String(255), nullable=False),
        sa.Column('repo_name', sa.String(255), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(500), nullable=False),
        sa.Column('predicted_line', sa.Integer(), nullable=True),
        sa.Column('concern', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('source_comment_id', sa.BigInteger(), nullable=True),
        sa.Column('matched', sa.Boolean(), nullable=True),
        sa.Column('match_type', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False)
    )
    op.create_index(
        'ix_predictions_repo',
        'predictions',
        ['repo_owner', 'repo_name', 'pr_number']
    )


def downgrade() -> None:
    op.drop_index('ix_predictions_repo', table_name='predictions')
    op.drop_table('predictions')