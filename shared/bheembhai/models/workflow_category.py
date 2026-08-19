"""WorkflowCategory model — global reference data grouping workflows."""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    UUID,
    DateTime,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base

if TYPE_CHECKING:
    from bheembhai.models.workflow import Workflow


class WorkflowCategory(Base):
    __tablename__ = "workflow_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    workflows: Mapped[list["Workflow"]] = relationship(back_populates="workflow_category")

    __table_args__ = (
        UniqueConstraint("name", name="uq_workflow_categories_name"),
    )
