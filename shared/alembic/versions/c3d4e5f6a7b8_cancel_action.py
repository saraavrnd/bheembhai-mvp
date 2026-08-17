"""Allow `cancel` work-queue actions (stop-run).

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-16

Stop-run: the platform enqueues a `cancel` item (bookkeeper, ADR-003); the
engine worker claims it, signals the run's in-flight dispatch through an
in-memory event (or, with no live dispatch, transitions the run to the
already-terminal-domain state `cancelled` itself) and voids queued siblings.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_work_queue_action", "work_queue", type_="check")
    op.create_check_constraint(
        "ck_work_queue_action", "work_queue",
        "action IN ('start', 'continue', 'cancel')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_work_queue_action", "work_queue", type_="check")
    op.create_check_constraint(
        "ck_work_queue_action", "work_queue",
        "action IN ('start', 'continue')",
    )
