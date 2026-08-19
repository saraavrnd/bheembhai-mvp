"""Create workflow_categories table + workflows.workflow_category_id FK.

Revision ID: f0a1b2c3d4e5
Revises: e7f8a9b0c1d2
Create Date: 2026-08-18

Workflow categories are global reference data (not project-scoped): a
platform workflow and its project copies share the same category row, so the
FK is plain RESTRICT. The column is nullable — pre-existing workflows and
copies simply stay uncategorized. Deletes of an in-use category are blocked
by the admin endpoint with a 409; RESTRICT is the DB-level safety net.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_categories",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_workflow_categories_name"),
    )
    op.add_column(
        "workflows",
        sa.Column("workflow_category_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "workflows_workflow_category_id_fkey",
        "workflows", "workflow_categories",
        ["workflow_category_id"], ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "workflows_workflow_category_id_fkey", "workflows", type_="foreignkey"
    )
    op.drop_column("workflows", "workflow_category_id")
    op.drop_table("workflow_categories")
