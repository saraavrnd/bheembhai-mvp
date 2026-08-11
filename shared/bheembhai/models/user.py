"""User and membership models."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import UUID, Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bheembhai.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    auth_provider: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    platform_role: Mapped[str] = mapped_column(
        String(20), nullable=False, default="USER", server_default="USER"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")

    __table_args__ = (
        UniqueConstraint("external_id", "auth_provider", name="uq_users_external_provider"),
    )


class ProjectRole(Base):
    __tablename__ = "project_roles"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )


class Membership(Base):
    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(
        Text, ForeignKey("project_roles.key"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc),
        server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="memberships")
    project: Mapped["Project"] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_membership_user_project"),
    )
