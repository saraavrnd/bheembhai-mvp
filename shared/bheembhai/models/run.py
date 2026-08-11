"""Run, Step, and Transition models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (UUID, BigInteger, DateTime, ForeignKey, Integer, Numeric,
                        String, Text, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policies.id"), nullable=False
    )
    story_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_branch: Mapped[str] = mapped_column(Text, nullable=False)
    run_branch: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    current_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="runs")
    workflow: Mapped["Workflow"] = relationship(back_populates="runs")
    policy: Mapped["Policy"] = relationship(back_populates="runs")
    steps: Mapped[list["Step"]] = relationship(back_populates="run")
    transitions: Mapped[list["Transition"]] = relationship(back_populates="run")
    work_queue_items: Mapped[list["WorkQueueItem"]] = relationship(back_populates="run")


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    skill: Mapped[str] = mapped_column(Text, nullable=False)
    exec_state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    result_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_requested: Mapped[str | None] = mapped_column(Text, nullable=True)
    models_used: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0, server_default="0"
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    fargate_task_arn: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped["Run"] = relationship(back_populates="steps")


class Transition(Base):
    __tablename__ = "transitions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id"), nullable=False
    )
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    from_state: Mapped[str] = mapped_column(Text, nullable=False)
    to_state: Mapped[str] = mapped_column(Text, nullable=False)
    result_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(
        Text, nullable=False, default="system", server_default="system"
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[float] = mapped_column(Numeric, nullable=False)

    run: Mapped["Run"] = relationship(back_populates="transitions")
