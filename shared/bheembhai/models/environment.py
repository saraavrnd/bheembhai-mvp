"""Environment variable model — platform- and project-scoped container env.

Plain rows store `value`; secret rows store an opaque SecureStorage
`credential_ref` (ADR-012 — the raw secret never lives in Postgres).
Platform rows carry `project_id = NULL`; project rows override same-named
platform rows at resolution time (engine-side, see `bheembhai.env_vars`).
"""

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    UUID,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base

if TYPE_CHECKING:
    from bheembhai.models.project import Project


class EnvironmentVariable(Base):
    __tablename__ = "environment_variables"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 'platform' (project_id NULL) or 'project' (project_id set) — the
    # ck_envvar_scope_project constraint keeps the two in lockstep.
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    # 'plain' (value) or 'secret' (credential_ref) — the
    # ck_envvar_value_or_ref constraint keeps the two in lockstep.
    value_type: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project: Mapped["Project"] = relationship(passive_deletes=True)

    __table_args__ = (
        CheckConstraint(
            "scope IN ('platform', 'project')",
            name="ck_envvar_scope",
        ),
        CheckConstraint(
            "value_type IN ('plain', 'secret')",
            name="ck_envvar_value_type",
        ),
        CheckConstraint(
            "(scope = 'platform' AND project_id IS NULL)"
            " OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_envvar_scope_project",
        ),
        CheckConstraint(
            "(value_type = 'plain' AND value IS NOT NULL AND credential_ref IS NULL)"
            " OR (value_type = 'secret' AND credential_ref IS NOT NULL AND value IS NULL)",
            name="ck_envvar_value_or_ref",
        ),
        # Postgres treats NULLs as distinct, so platform rows (project_id
        # NULL) are also covered: unique name per project + unique platform
        # name. A project row sharing a platform row's name is the override.
        UniqueConstraint("project_id", "name", name="uq_envvar_project_name"),
    )
