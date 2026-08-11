"""Workflow and Policy models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (UUID, Boolean, DateTime, ForeignKey, Integer, Text,
                        UniqueConstraint, func)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    yaml_content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    project: Mapped["Project"] = relationship(back_populates="workflows")
    policies: Mapped[list["Policy"]] = relationship(back_populates="workflow")
    runs: Mapped[list["Run"]] = relationship(back_populates="workflow")

    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_workflow_project_name_ver"),
    )


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workflows.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    yaml_content: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    workflow: Mapped["Workflow"] = relationship(back_populates="policies")
    runs: Mapped[list["Run"]] = relationship(back_populates="policy")

    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_policy_project_name_ver"),
    )
