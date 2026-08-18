"""Add ON DELETE CASCADE to work_queue.run_id FK.

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4d5e6f7a8b9c"
down_revision: str | None = "3c4d5e6f7a8b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("work_queue_run_id_fkey", "work_queue", type_="foreignkey")
    op.create_foreign_key(
        "work_queue_run_id_fkey",
        "work_queue", "runs",
        ["run_id"], ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("work_queue_run_id_fkey", "work_queue", type_="foreignkey")
    op.create_foreign_key(
        "work_queue_run_id_fkey",
        "work_queue", "runs",
        ["run_id"], ["id"],
    )
