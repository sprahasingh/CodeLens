import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class FailedPR(Base):
    """A PR whose processing exhausted all Celery retries. Durable record so
    permanently-failed jobs are queryable and replayable, instead of only
    existing as a log line."""

    __tablename__ = "failed_prs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_owner: Mapped[str] = mapped_column(String(255))
    repo_name: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    failed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
