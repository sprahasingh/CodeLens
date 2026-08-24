"""add hnsw index on review_comments embedding

Revision ID: 822f8635a9f2
Revises: 3466cf94319a
Create Date: 2026-08-25 01:36:27.606532

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '822f8635a9f2'
down_revision: Union[str, Sequence[str], None] = '3466cf94319a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.execute("""
        CREATE INDEX IF NOT EXISTS review_comments_embedding_hnsw_idx
        ON review_comments
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade():
    op.execute("DROP INDEX IF EXISTS review_comments_embedding_hnsw_idx")