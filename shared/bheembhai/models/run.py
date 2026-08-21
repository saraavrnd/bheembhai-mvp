"""Run, Step, and Transition models."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    UUID,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base

if TYPE_CHECKING:
    from bheembhai.models.project import Project
    from bheembhai.models.work_queue import WorkQueueItem
    from bheembhai.models.workflow import Policy, Workflow


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    policy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("policies.id"), nullable=False
    )
    story_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_branch: Mapped[str] = mapped_column(Text, nullable=False)
    run_branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_integrations.id", ondelete="SET NULL"),
        nullable=True
    )
    jira_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_integrations.id", ondelete="SET NULL"),
        nullable=True
    )
    ai_vendor_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("project_integrations.id", ondelete="SET NULL"),
        nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending", server_default="pending"
    )
    current_step: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0, server_default="0"
    )
    # Who submitted the run — set at creation; SET NULL keeps history if the
    # user is later deleted. Resolved via a users lookup, not a relationship.
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Ad-hoc sessions (ADR-016): run_kind discriminates the governed pipeline
    # ("workflow") from a free-form user-query session ("adhoc"). The query
    # persists on the run so it survives engine restarts; the session columns
    # carry the Claude Code --session-id and the reaper's lifecycle clock.
    run_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="workflow", server_default="workflow"
    )
    user_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_phase: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    session_last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
    # Skill bundle pin (Phase 1): frozen at init so mid-run skill edits can
    # never change an in-flight step; each launch presigns a fresh GET for
    # the pinned key. NULL = pre-migration rows (backfilled by the engine on
    # non-first-init dispatches).
    skill_s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
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
    # Structured detail that survives restarts (ADR-003 durability): step outcomes
    # (summary/artifact/files) on completion rows and the gate card on
    # awaiting_approval rows — the engine rebuilds routing + re-notification from
    # these after a crash. UI contract unchanged (renders from this same payload).
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[float] = mapped_column(Numeric, nullable=False)

    run: Mapped["Run"] = relationship(back_populates="transitions")


class RunLog(Base):
    """Object-storage reference for one step attempt's log artifact (ADR-011).

    Content lives in object storage under ``logs/<run_id>/<step_id>/<attempt_no>/``
    (key built by ``bheembhai.log_keys.log_key``). This row is the durable
    pointer + size: the platform serves logs from it without scanning storage,
    and the reference commits in the SAME transaction as the step's transition
    so a crash can never leave an unpointed artifact (re-upload on re-entry is
    idempotent — the unique constraint dedupes)."""
    __tablename__ = "run_logs"
    __table_args__ = (
        UniqueConstraint("run_id", "step_id", "attempt_no", "kind",
                         name="uq_run_logs_attempt_kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False, index=True
    )
    step_id: Mapped[str] = mapped_column(Text, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )
