"""Add ON DELETE CASCADE to runs.project_id and transitions.run_id FKs.

Revision ID: 3c4d5e6f7a8b
Revises: 2a1b3c4d5e6f
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c4d5e6f7a8b"
down_revision: str | None = "2a1b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # runs.project_id: drop old FK, recreate with ON DELETE CASCADE
    op.drop_constraint("runs_project_id_fkey", "runs", type_="foreignkey")
    op.create_foreign_key(
        "runs_project_id_fkey",
        "runs", "projects",
        ["project_id"], ["id"],
        ondelete="CASCADE",
    )

    # transitions.run_id: drop old FK, recreate with ON DELETE CASCADE
    op.drop_constraint("transitions_run_id_fkey", "transitions", type_="foreignkey")
    op.create_foreign_key(
        "transitions_run_id_fkey",
        "transitions", "runs",
        ["run_id"], ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    # Revert runs.project_id: drop CASCADE FK, recreate without
    op.drop_constraint("runs_project_id_fkey", "runs", type_="foreignkey")
    op.create_foreign_key(
        "runs_project_id_fkey",
        "runs", "projects",
        ["project_id"], ["id"],
    )

    # Revert transitions.run_id: drop CASCADE FK, recreate without
    op.drop_constraint("transitions_run_id_fkey", "transitions", type_="foreignkey")
    op.create_foreign_key(
        "transitions_run_id_fkey",
        "transitions", "runs",
        ["run_id"], ["id"],
    )
