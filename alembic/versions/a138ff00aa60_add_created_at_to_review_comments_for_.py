"""add created at to review comments for chronological eval split

Revision ID: a138ff00aa60
Revises: 7823efbb00a2
Create Date: 2026-09-14 21:58:50.045306

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a138ff00aa60'
down_revision: Union[str, Sequence[str], None] = '7823efbb00a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'review_comments',
        sa.Column('comment_created_at', sa.DateTime(), nullable=True)
    )
    op.create_index(
        'ix_review_comments_created_at',
        'review_comments',
        ['repo_owner', 'repo_name', 'comment_created_at']
    )


def downgrade() -> None:
    op.drop_index('ix_review_comments_created_at', table_name='review_comments')
    op.drop_column('review_comments', 'comment_created_at')