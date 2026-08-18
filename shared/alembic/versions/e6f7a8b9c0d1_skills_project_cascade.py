"""Skills.project_id FK: SET NULL → CASCADE on project delete.

Revision ID: e6f7a8b9c0d1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-18

e5f6a7b8c9d0 created the FK with ondelete="SET NULL" (mirroring workflows).
That breaks admin project deletion for any project whose skill shadows a
platform skill of the same name: the SET NULL re-parents the project row into
platform scope (project_id → NULL) and the INSERT-like move collides with the
partial unique index uq_skills_platform_name (name) WHERE project_id IS NULL.

Project skills are project-specific clones/edits — they die with their
project. Platform templates are untouched.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("fk_skills_project_id_projects", "skills", type_="foreignkey")
    op.create_foreign_key(
        "fk_skills_project_id_projects", "skills", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_skills_project_id_projects", "skills", type_="foreignkey")
    op.create_foreign_key(
        "fk_skills_project_id_projects", "skills", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )
