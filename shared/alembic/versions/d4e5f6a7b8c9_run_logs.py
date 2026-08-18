"""Add run_logs — object-storage references for step attempt logs.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-17

The engine uploads each attempt's agent.log / container.log / diagnostics.txt
to object storage (ADR-011) and records a reference row in the same
transaction as the step's terminal transition. The platform serves logs from
the reference — storage is never scanned. Content itself never lives in
Postgres. UNIQUE(run_id, step_id, attempt_no, kind) makes crash-recovery
re-upload idempotent.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "step_id", "attempt_no", "kind",
            name="uq_run_logs_attempt_kind"
        ),
    )
    op.create_index("ix_run_logs_run_id", "run_logs", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_run_logs_run_id", table_name="run_logs")
    op.drop_table("run_logs")
