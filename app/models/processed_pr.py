import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, UniqueConstraint
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
