"""Add skills.s3_key + skills.sha256 (S3 skill bundles, Phase 1).

Revision ID: a7b8c9d0e1f2
Revises: f2c3d4e5f6a7
Create Date: 2026-08-19

Every skill-content write publishes a deterministic tar.gz bundle to object
storage; the row stores the content-addressed object key + sha256. NULL =
never published — legacy rows are self-healed by the engine at run init.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("skills", sa.Column("s3_key", sa.Text(), nullable=True))
    op.add_column("skills", sa.Column("sha256", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("skills", "sha256")
    op.drop_column("skills", "s3_key")
