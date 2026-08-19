"""Add projects.description column.

Revision ID: f2c3d4e5f6a7
Revises: e9f8a7b6c5d4
Create Date: 2026-08-18

The Configuration → Details sub-tab lets project managers edit the project
description, but the Project model never stored one. server_default=""
backfills existing rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2c3d4e5f6a7"
down_revision: str | None = "e9f8a7b6c5d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("projects", "description")
