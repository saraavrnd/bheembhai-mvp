"""Record who initiated each run.

Revision ID: a1b2c3d4e5f7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-13

Adds ``runs.started_by_user_id`` — a nullable FK to ``users.id`` captured at
run submission. SET NULL on user deletion keeps run history intact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("started_by_user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_runs_started_by_user_id", "runs", "users",
        ["started_by_user_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_runs_started_by_user_id", "runs", type_="foreignkey")
    op.drop_column("runs", "started_by_user_id")
