"""add match_reason to predictions for judge audit trail

Revision ID: e871bb2b6bca
Revises: 3015c46a714f
Create Date: 2026-09-26 04:19:57.128712

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e871bb2b6bca'
down_revision: Union[str, Sequence[str], None] = '3015c46a714f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'predictions',
        sa.Column('match_reason', sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('predictions', 'match_reason')
