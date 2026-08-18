"""user-enabled-flag

Revision ID: 2a1b3c4d5e6f
Revises: 698bb9bb1663
Create Date: 2026-08-12 10:00:00.000000

Add is_enabled boolean column to users table — default True so existing users
remain active and new users are enabled by default.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2a1b3c4d5e6f'
down_revision: str | None = '698bb9bb1663'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column(
            'is_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    op.drop_column('users', 'is_enabled')
