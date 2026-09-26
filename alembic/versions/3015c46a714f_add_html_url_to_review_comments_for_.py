"""add html_url to review_comments for provenance links

Revision ID: 3015c46a714f
Revises: 2386cbeb1324
Create Date: 2026-09-26 04:11:14.817214

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3015c46a714f'
down_revision: Union[str, Sequence[str], None] = '2386cbeb1324'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'review_comments',
        sa.Column('html_url', sa.String(500), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('review_comments', 'html_url')
