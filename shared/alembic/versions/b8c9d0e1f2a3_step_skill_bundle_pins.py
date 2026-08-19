"""Add steps.skill_s3_key + steps.skill_sha256 (skill bundle pins, Phase 1).

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-19

The engine freezes the skill bundle key onto each Step row at first init so
mid-run skill edits never change an in-flight step (each launch presigns a
fresh GET for the pinned key). NULL = pre-migration rows — backfilled by the
engine on non-first-init dispatches.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("steps", sa.Column("skill_s3_key", sa.Text(), nullable=True))
    op.add_column("steps", sa.Column("skill_sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("steps", "skill_sha256")
    op.drop_column("steps", "skill_s3_key")
