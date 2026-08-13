"""Expand integration type check constraint to include AI vendors.

Revision ID: 9f0a9b1c2d3e
Revises: 8f8a9b0c1d3e
Create Date: 2026-08-12

Adds 'openai', 'claude', 'deepseek', 'kimi' to the allowed integration types.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "9f0a9b1c2d3e"
down_revision: Union[str, None] = "8f8a9b0c1d3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old two-type constraint
    op.execute("ALTER TABLE project_integrations DROP CONSTRAINT IF EXISTS ck_integration_type")

    # Add the expanded six-type constraint
    op.execute(
        "ALTER TABLE project_integrations ADD CONSTRAINT ck_integration_type "
        "CHECK (type IN ('github', 'jira', 'openai', 'claude', 'deepseek', 'kimi'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE project_integrations DROP CONSTRAINT IF EXISTS ck_integration_type")
    op.execute(
        "ALTER TABLE project_integrations ADD CONSTRAINT ck_integration_type "
        "CHECK (type IN ('github', 'jira'))"
    )
