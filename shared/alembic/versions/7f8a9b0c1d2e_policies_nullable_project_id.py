"""Make policies.project_id nullable and update unique constraint.

Revision ID: 7f8a9b0c1d2e
Revises: 6f7a8b9c0d1e
Create Date: 2026-08-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "7f8a9b0c1d2e"
down_revision: Union[str, None] = "6f7a8b9c0d1e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop old FK (CASCADE → SET NULL)
    op.drop_constraint("policies_project_id_fkey", "policies", type_="foreignkey")

    # 2. Drop old unique constraint (project_id, name, version) → (workflow_id, name, version)
    op.drop_constraint("uq_policy_project_name_ver", "policies", type_="unique")

    # 3. Make project_id nullable
    op.alter_column("policies", "project_id", nullable=True)

    # 4. Re-create FK with ON DELETE SET NULL
    op.create_foreign_key(
        "policies_project_id_fkey",
        "policies", "projects",
        ["project_id"], ["id"],
        ondelete="SET NULL",
    )

    # 5. New unique constraint on (workflow_id, name, version)
    op.create_unique_constraint(
        "uq_policy_workflow_name_ver", "policies",
        ["workflow_id", "name", "version"],
    )


def downgrade() -> None:
    # Reverse
    op.drop_constraint("uq_policy_workflow_name_ver", "policies", type_="unique")
    op.drop_constraint("policies_project_id_fkey", "policies", type_="foreignkey")
    op.alter_column("policies", "project_id", nullable=False)
    op.create_foreign_key(
        "policies_project_id_fkey",
        "policies", "projects",
        ["project_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_policy_project_name_ver", "policies",
        ["project_id", "name", "version"],
    )
