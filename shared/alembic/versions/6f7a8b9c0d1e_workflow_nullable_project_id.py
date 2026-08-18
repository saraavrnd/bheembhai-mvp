"""Make workflow/project_id and policy/project_id nullable — independent templates.

Revision ID: 6f7a8b9c0d1e
Revises: 5e6f7a8b9c0d
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6f7a8b9c0d1e"
down_revision: str | None = "5e6f7a8b9c0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ═══ Workflows ═══

    # 1. Drop old FK (CASCADE → we want SET NULL instead)
    op.drop_constraint("workflows_project_id_fkey", "workflows", type_="foreignkey")

    # 2. Drop old unique constraint (project_id, name, version) → (name, version)
    op.drop_constraint("uq_workflow_project_name_ver", "workflows", type_="unique")

    # 3. Make project_id nullable
    op.alter_column("workflows", "project_id", nullable=True)

    # 4. Re-create FK with ON DELETE SET NULL
    op.create_foreign_key(
        "workflows_project_id_fkey",
        "workflows", "projects",
        ["project_id"], ["id"],
        ondelete="SET NULL",
    )

    # 5. New unique constraint on (name, version) only
    op.create_unique_constraint("uq_workflow_name_ver", "workflows", ["name", "version"])


def downgrade() -> None:
    # Best-effort: remove rows with NULL project_id
    op.execute("DELETE FROM policies WHERE workflow_id IN (SELECT id FROM workflows WHERE project_id IS NULL)")
    op.execute("DELETE FROM runs WHERE workflow_id IN (SELECT id FROM workflows WHERE project_id IS NULL)")
    op.execute("DELETE FROM workflows WHERE project_id IS NULL")

    op.drop_constraint("uq_workflow_name_ver", "workflows", type_="unique")
    op.drop_constraint("workflows_project_id_fkey", "workflows", type_="foreignkey")
    op.alter_column("workflows", "project_id", nullable=False)
    op.create_foreign_key(
        "workflows_project_id_fkey",
        "workflows", "projects",
        ["project_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint("uq_workflow_project_name_ver", "workflows", ["project_id", "name", "version"])
