import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, BigInteger, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column
from pgvector.sqlalchemy import Vector
from app.core.database import Base
from app.services.embedder import EMBEDDING_DIMENSION


class ReviewComment(Base):
    __tablename__ = "review_comments"

    id: Mapped[str] = mapped_column(
        String, primary_key=True,
        default=lambda: str(uuid.uuid4())
    )
    github_comment_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    repo_owner: Mapped[str] = mapped_column(String(255))
    repo_name: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    line: Mapped[int] = mapped_column(Integer, nullable=True)
    diff_hunk: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(255))
    embedding: Mapped[list] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)