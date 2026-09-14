import uuid
from datetime import datetime
from sqlalchemy import String, Integer, Float, Text, DateTime, BigInteger, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    repo_owner: Mapped[str] = mapped_column(String(255))
    repo_name: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(String(500))
    predicted_line: Mapped[int] = mapped_column(Integer, nullable=True)
    concern: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    source_comment_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    matched: Mapped[bool] = mapped_column(Boolean, nullable=True)
    match_type: Mapped[str] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)