"""Ad-hoc session columns on runs.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-21

Adds the run columns the ad-hoc session feature needs:

- ``run_kind`` — discriminator: ``workflow`` (the governed pipeline, default)
  or ``adhoc`` (free-form user query on a user-named branch).
- ``user_query`` — the user's query, persisted on the run so it survives
  engine restarts (the per-step context re-materializes it each launch).
- ``claude_session_id`` — the Claude Code ``--session-id`` the engine mints
  for resumable sessions (Phase 3); nullable, set at first launch.
- ``session_phase`` — the ad-hoc session lifecycle: ``pending`` before first
  launch, ``active`` while a live container owns the session, ``ended`` once
  the session closes (explicit End or idle reap).
- ``session_last_activity_at`` — last turn/launch timestamp; the idle reaper
  compares it against ``BB_ADHOC_IDLE_SECONDS``.

All columns land in one migration so Phase 1-3 ship without further schema
changes. Existing rows keep ``run_kind='workflow'``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column(
        "run_kind", sa.String(16), nullable=False, server_default="workflow"))
    op.add_column("runs", sa.Column("user_query", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("claude_session_id", sa.String(36), nullable=True))
    op.add_column("runs", sa.Column(
        "session_phase", sa.String(16), nullable=False, server_default="pending"))
    op.add_column("runs", sa.Column(
        "session_last_activity_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "session_last_activity_at")
    op.drop_column("runs", "session_phase")
    op.drop_column("runs", "claude_session_id")
    op.drop_column("runs", "user_query")
    op.drop_column("runs", "run_kind")
