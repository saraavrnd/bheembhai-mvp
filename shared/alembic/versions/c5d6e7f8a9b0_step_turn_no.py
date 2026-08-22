"""Per-step turn counter for ad-hoc sessions (ADR-016 Phase 2).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-21

Adds ``steps.turn_no`` — the global-monotonic turn sequence for ad-hoc session
steps. The engine increments it per user turn BEFORE writing the turn's inbox
object, and the container echoes it in the outbox reply. A counter that spans
attempts (container incarnations) keeps the inbox/outbox ``seq`` stable across
cold-start relaunches; the step's ``attempt_no`` stays the container-incarnation
number. Workflow steps never use it (stays 0).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("steps", sa.Column(
        "turn_no", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("steps", "turn_no")
