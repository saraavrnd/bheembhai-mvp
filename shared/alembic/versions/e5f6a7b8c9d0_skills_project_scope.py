"""Skills project scope: nullable project_id + partial unique indexes.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-18

Platform skills (project_id IS NULL): unique on (name).
Project skills (project_id IS NOT NULL): unique on (project_id, name).
Project rows shadow platform rows by name at run time (engine resolves
project-first), and each project keeps its own edited copy of a platform
template.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Nullable project scope column + FK (SET NULL keeps skills alive if
    # their project is deleted).
    op.add_column("skills", sa.Column("project_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_skills_project_id_projects", "skills", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )

    # 2. Drop the global name uniqueness — replaced by the two partial indexes.
    op.drop_constraint("uq_skills_name", "skills", type_="unique")

    # 3. Platform skills: unique on (name) where project_id IS NULL
    op.execute(
        "CREATE UNIQUE INDEX uq_skills_platform_name ON skills (name) "
        "WHERE project_id IS NULL"
    )

    # 4. Project skills: unique on (project_id, name) where project_id IS NOT NULL
    op.execute(
        "CREATE UNIQUE INDEX uq_skills_project_name ON skills (project_id, name) "
        "WHERE project_id IS NOT NULL"
    )


def downgrade() -> None:
    # NOTE: fails if any project-scoped skill rows exist (their names can
    # collide with platform names) — only valid while none have been created.
    op.execute("DROP INDEX IF EXISTS uq_skills_project_name")
    op.execute("DROP INDEX IF EXISTS uq_skills_platform_name")
    op.create_unique_constraint("uq_skills_name", "skills", ["name"])
    op.drop_constraint("fk_skills_project_id_projects", "skills", type_="foreignkey")
    op.drop_column("skills", "project_id")
