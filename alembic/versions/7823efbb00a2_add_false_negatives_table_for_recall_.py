"""add false negatives table for recall tracking

Revision ID: 7823efbb00a2
Revises: a1f45e9be81a
Create Date: 2026-09-14 18:43:22.557130

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7823efbb00a2'
down_revision: Union[str, Sequence[str], None] = 'a1f45e9be81a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'false_negatives',
        sa.Column('id', sa.String(), primary_key=True),
        sa.Column('repo_owner', sa.String(255), nullable=False),
        sa.Column('repo_name', sa.String(255), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('path', sa.String(500), nullable=False),
        sa.Column('comment_line', sa.Integer(), nullable=True),
        sa.Column('comment_body', sa.Text(), nullable=False),
        sa.Column('source_comment_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False)
    )
    op.create_index(
        'ix_false_negatives_repo',
        'false_negatives',
        ['repo_owner', 'repo_name', 'pr_number']
    )


def downgrade() -> None:
    op.drop_index('ix_false_negatives_repo', table_name='false_negatives')
    op.drop_table('false_negatives')