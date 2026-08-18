"""Workflow and Policy models."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    UUID,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base

if TYPE_CHECKING:
    from bheembhai.models.project import Project
    from bheembhai.models.run import Run


class Workflow(Base):
    __tablename__ = "workflows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
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
        # Platform workflows (project_id IS NULL): unique on (name, version)
        # Project workflows (project_id IS NOT NULL): unique on (project_id, name, version)
        # Implemented via partial unique indexes in migration 8f8a9b0c1d3e.
        # project_id deletes CASCADE (e7f8a9b0c1d2): SET NULL would re-parent a
        # copied workflow into platform scope and collide with the platform
        # (name, version) index whenever it shares the template's identity.
    )


class Policy(Base):
    __tablename__ = "policies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
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
        UniqueConstraint("workflow_id", "name", "version", name="uq_policy_workflow_name_ver"),
        # project_id deletes CASCADE (e7f8a9b0c1d2): a surviving project policy
        # would dangle its NO ACTION workflow_id FK once the project's
        # workflows cascade — project policies die with their project.
    )
