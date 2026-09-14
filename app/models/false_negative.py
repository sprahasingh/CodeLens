import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime, BigInteger
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class FalseNegative(Base):
    __tablename__ = "false_negatives"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_owner: Mapped[str] = mapped_column(String(255))
    repo_name: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    comment_line: Mapped[int] = mapped_column(Integer, nullable=True)
    comment_body: Mapped[str] = mapped_column(Text)
    source_comment_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)