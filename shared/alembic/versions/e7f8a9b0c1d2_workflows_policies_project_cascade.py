"""Workflows/policies project_id FKs: SET NULL → CASCADE on project delete.

Revision ID: e7f8a9b0c1d2
Revises: e6f7a8b9c0d1
Create Date: 2026-08-18

Both FKs were SET NULL (mirroring the old "orphans become platform
templates" idea). That breaks admin project deletion in the normal case:

- workflows: copy-to-project clones a platform workflow into project scope
  with the SAME (name, version). Deleting the project SET NULLs the copy's
  project_id, re-parenting it into platform scope where it collides with the
  partial unique index uq_workflow_platform_name_ver (name, version)
  WHERE project_id IS NULL → UniqueViolationError on the DELETE.
- policies: once workflows CASCADE, a surviving project policy would dangle
  (policies.workflow_id is a NO ACTION FK to workflows) and the delete would
  fail with an FK violation instead.

Project-scoped copies die with their project — the platform template rows
(project_id IS NULL) are untouched. Matches the skills decision in
e6f7a8b9c0d1.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("workflows_project_id_fkey", "workflows", type_="foreignkey")
    op.create_foreign_key(
        "workflows_project_id_fkey", "workflows", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )
    op.drop_constraint("policies_project_id_fkey", "policies", type_="foreignkey")
    op.create_foreign_key(
        "policies_project_id_fkey", "policies", "projects",
        ["project_id"], ["id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("workflows_project_id_fkey", "workflows", type_="foreignkey")
    op.create_foreign_key(
        "workflows_project_id_fkey", "workflows", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )
    op.drop_constraint("policies_project_id_fkey", "policies", type_="foreignkey")
    op.create_foreign_key(
        "policies_project_id_fkey", "policies", "projects",
        ["project_id"], ["id"], ondelete="SET NULL",
    )
