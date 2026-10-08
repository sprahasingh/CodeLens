import uuid
from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, DateTime, UniqueConstraint, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class ProcessedPR(Base):
    __tablename__ = "processed_prs"
    __table_args__ = (
        UniqueConstraint(
            "repo_owner", "repo_name", "pr_number", "head_sha",
            name="uq_processed_prs_repo_pr_sha"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_owner: Mapped[str] = mapped_column(String(255))
    repo_name: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    task_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    lease_owner: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dispatch_attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    review_payload: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dispatched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
