"""Project and integration models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (UUID, Boolean, CheckConstraint, DateTime, ForeignKey,
                        String, Text, UniqueConstraint, func)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    # passive_deletes: the DB FK constraints cascade (memberships/integrations/
    # runs: ON DELETE CASCADE) or null (workflows: ON DELETE SET NULL) — the
    # ORM must not emulate the cascade, because its emulation nulls NOT NULL
    # FKs (UPDATE project_integrations SET project_id=NULL) and blows up on
    # admin project deletion.
    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="project", passive_deletes=True)
    integrations: Mapped[list["ProjectIntegration"]] = relationship(
        back_populates="project", passive_deletes=True)
    workflows: Mapped[list["Workflow"]] = relationship(
        back_populates="project", passive_deletes=True)
    runs: Mapped[list["Run"]] = relationship(
        back_populates="project", passive_deletes=True)


class ProjectIntegration(Base):
    __tablename__ = "project_integrations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)
    credential_ref: Mapped[str] = mapped_column(
        Text, nullable=False
    )
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="integrations")

    __table_args__ = (
        CheckConstraint(
            "type IN ('github', 'jira', 'openai', 'claude', 'deepseek', 'kimi')",
            name="ck_integration_type",
        ),
        UniqueConstraint(
            "project_id", "type", "label",
            name="uq_integration_project_type_label",
        ),
    )
