"""Fix workflow unique constraints: use partial indexes for platform vs project.

Revision ID: 8f8a9b0c1d3e
Revises: 7f8a9b0c1d2e
Create Date: 2026-08-12

Platform workflows (project_id IS NULL): unique on (name, version).
Project workflows (project_id IS NOT NULL): unique on (project_id, name, version).
This allows cloning: a platform template and its project copy can share name+version.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8f8a9b0c1d3e"
down_revision: str | None = "7f8a9b0c1d2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the global (name, version) unique constraint
    op.drop_constraint("uq_workflow_name_ver", "workflows", type_="unique")

    # 2. Partial index: platform workflows (project_id IS NULL) — unique on (name, version)
    op.execute(
        "CREATE UNIQUE INDEX uq_workflow_platform_name_ver ON workflows (name, version) "
        "WHERE project_id IS NULL"
    )

    # 3. Partial index: project workflows (project_id IS NOT NULL) — unique on (project_id, name, version)
    op.execute(
        "CREATE UNIQUE INDEX uq_workflow_project_name_ver ON workflows (project_id, name, version) "
        "WHERE project_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_workflow_project_name_ver")
    op.execute("DROP INDEX IF EXISTS uq_workflow_platform_name_ver")
    op.create_unique_constraint(
        "uq_workflow_name_ver", "workflows", ["name", "version"]
    )
