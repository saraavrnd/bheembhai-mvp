"""Work queue model — Postgres-backed FIFO queue (ADR-003)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (UUID, BigInteger, CheckConstraint, DateTime, ForeignKey,
                        String, Text, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base


class WorkQueueItem(Base):
    __tablename__ = "work_queue"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    run: Mapped["Run"] = relationship(back_populates="work_queue_items")

    __table_args__ = (
        CheckConstraint(
            "action IN ('start', 'continue', 'cancel')",
            name="ck_work_queue_action",
        ),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'done')",
            name="ck_work_queue_state",
        ),
    )
