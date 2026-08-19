"""Skill library models — skills and their files."""

import uuid
from datetime import datetime, timezone

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


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(
        Text, nullable=False, default="medium", server_default="medium"
    )
    compatibility: Mapped[str | None] = mapped_column(Text, nullable=True)
    # S3 bundle (Phase 1): content-addressed object key + sha256 of the
    # deterministic tar.gz this row's current content packs to. NULL = never
    # published (legacy rows — the engine self-heals at run init).
    s3_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    files: Mapped[list["SkillFile"]] = relationship(
        back_populates="skill", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "model IN ('high', 'medium', 'low')",
            name="ck_skills_model",
        ),
        # Platform skills (project_id IS NULL): unique on (name)
        # Project skills (project_id IS NOT NULL): unique on (project_id, name)
        # Implemented via partial unique indexes in migration e5f6a7b8c9d0.
        # project_id deletes CASCADE (e6f7a8b9c0d1): SET NULL would re-parent a
        # project skill into platform scope and collide with the platform-name
        # index whenever it shadows a platform skill of the same name.
    )


class SkillFile(Base):
    __tablename__ = "skill_files"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id", ondelete="CASCADE"),
        nullable=False
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    skill: Mapped["Skill"] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint(
            "skill_id", "path",
            name="uq_skill_files_skill_path",
        ),
    )
