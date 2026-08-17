"""Add structured payload to transitions.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f7
Create Date: 2026-08-14

Adds ``transitions.payload`` JSONB (nullable). The engine stores step-outcome
detail (summary/artifact/files) on completion rows and the gate card on
``awaiting_approval`` rows, so a crash mid-dispatch can be healed purely from
persisted state (ADR-003) and a gate re-notified after an engine restart.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transitions", sa.Column("payload", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("transitions", "payload")
