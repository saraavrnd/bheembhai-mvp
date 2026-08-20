"""Add environment_variables (platform + project scoped container env).

Revision ID: a3b4c5d6e7f8
Revises: b8c9d0e1f2a3
Create Date: 2026-08-20

Plain rows store `value`; secret rows store an opaque SecureStorage
`credential_ref` (ADR-012 — the raw secret never lives in Postgres).
Platform rows carry project_id NULL; the uq_envvar_project_name constraint
is NULL-distinct, so platform names are unique too. A project row sharing a
platform row's name is the override (engine merges project over platform).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "environment_variables",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("credential_ref", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "scope IN ('platform', 'project')", name="ck_envvar_scope"),
        sa.CheckConstraint(
            "value_type IN ('plain', 'secret')", name="ck_envvar_value_type"),
        sa.CheckConstraint(
            "(scope = 'platform' AND project_id IS NULL)"
            " OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_envvar_scope_project"),
        sa.CheckConstraint(
            "(value_type = 'plain' AND value IS NOT NULL AND credential_ref IS NULL)"
            " OR (value_type = 'secret' AND credential_ref IS NOT NULL AND value IS NULL)",
            name="ck_envvar_value_or_ref"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name",
                            name="uq_envvar_project_name"),
    )


def downgrade() -> None:
    op.drop_table("environment_variables")
