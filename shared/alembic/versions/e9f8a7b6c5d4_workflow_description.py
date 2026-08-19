"""Add workflows.description column.

Revision ID: e9f8a7b6c5d4
Revises: f0a1b2c3d4e5
Create Date: 2026-08-18

The Workflows catalog tab renders a one-line description per workflow card,
but the Workflow model never stored one (the admin create modal collected a
description that was silently discarded). server_default="" backfills
existing platform templates and project clones.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f8a7b6c5d4"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflows",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("workflows", "description")
