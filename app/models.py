from datetime import datetime, timezone, timedelta
from enum import StrEnum

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


def utcnow():
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint("status IN ('PENDING','RUNNING','SUCCESS','PARTIAL','FAILED')", name="job_status"),
        CheckConstraint("retry_count >= 0", name="job_retries"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= max_attempts AND max_attempts > 0", name="job_attempts"),
        UniqueConstraint("organization_id", "document_hash", "schema_hash", name="job_submission"),
    )

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default=JobState.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    organization_id: Mapped[str] = mapped_column(Text, default="")
    file_path: Mapped[str] = mapped_column(Text)
    field_schema: Mapped[dict] = mapped_column("schema", JSON().with_variant(JSONB, "postgresql"))
    document_hash: Mapped[str] = mapped_column(String(64))
    schema_hash: Mapped[str] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    error: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    result: Mapped[dict | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    lease_token: Mapped[str | None] = mapped_column(String(36))
    worker_id: Mapped[str | None] = mapped_column(String(36))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: utcnow() + timedelta(seconds=1800))
    queue_duration_ms: Mapped[int | None] = mapped_column(Integer)
    llm_usage: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list, server_default="[]")
    error_history: Mapped[list] = mapped_column(JSON().with_variant(JSONB, "postgresql"), default=list, server_default="[]")
