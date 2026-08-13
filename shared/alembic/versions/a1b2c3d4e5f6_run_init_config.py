"""ADR-013: run initialization schema — nullable run_branch, integration
selection FKs, high/medium/low skill tiers, workflow tier data migration.

Revision ID: a1b2c3d4e5f6
Revises: 9f0a9b1c2d3e
Create Date: 2026-08-13

- ``runs.run_branch`` becomes nullable (the engine derives and persists it).
- Three nullable FK columns on ``runs`` → ``project_integrations.id`` capture
  the integrations selected at run submission (GitHub required, Jira optional,
  AI vendor required — enforced at the API layer, not here).
- ``skills.model`` migrates {opus→high, sonnet→medium, haiku→low} with the
  check constraint replaced and the default moved to 'medium'.
- Existing workflow YAML ``model:`` values are rewritten to tiers.
- AI-vendor ``project_integrations.config`` key ``model_small`` → ``model_low``.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "9f0a9b1c2d3e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

AI_VENDOR_TYPES = ("openai", "claude", "deepseek", "kimi")


def upgrade() -> None:
    # ── runs: engine-owned branch + captured integration selections ──────────
    op.alter_column("runs", "run_branch", existing_type=sa.Text(), nullable=True)

    op.add_column("runs", sa.Column("github_integration_id", sa.UUID(), nullable=True))
    op.add_column("runs", sa.Column("jira_integration_id", sa.UUID(), nullable=True))
    op.add_column("runs", sa.Column("ai_vendor_integration_id", sa.UUID(), nullable=True))

    op.create_foreign_key(
        "fk_runs_github_integration_id", "runs", "project_integrations",
        ["github_integration_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_runs_jira_integration_id", "runs", "project_integrations",
        ["jira_integration_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_runs_ai_vendor_integration_id", "runs", "project_integrations",
        ["ai_vendor_integration_id"], ["id"], ondelete="SET NULL",
    )

    # ── skills: high/medium/low tiers ────────────────────────────────────────
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS ck_skills_model")
    op.execute("UPDATE skills SET model = 'high' WHERE model = 'opus'")
    op.execute("UPDATE skills SET model = 'medium' WHERE model = 'sonnet'")
    op.execute("UPDATE skills SET model = 'low' WHERE model = 'haiku'")
    op.execute("ALTER TABLE skills ALTER COLUMN model SET DEFAULT 'medium'")
    op.create_check_constraint(
        "ck_skills_model", "skills", "model IN ('high', 'medium', 'low')"
    )

    # ── workflows: per-step model tiers → high/medium/low ────────────────────
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'claude-opus-4-8', 'high')"
    )
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'claude-sonnet-4-6', 'medium')"
    )
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'claude-haiku-4-5', 'low')"
    )

    # ── integrations: model_small → model_low in AI vendor configs ───────────
    op.execute(
        "UPDATE project_integrations "
        "SET config = (config - 'model_small') || "
        "jsonb_build_object('model_low', config->'model_small') "
        "WHERE type IN ('openai', 'claude', 'deepseek', 'kimi') "
        "AND config ? 'model_small'"
    )


def downgrade() -> None:
    # ── integrations: model_low → model_small ────────────────────────────────
    op.execute(
        "UPDATE project_integrations "
        "SET config = (config - 'model_low') || "
        "jsonb_build_object('model_small', config->'model_low') "
        "WHERE type IN ('openai', 'claude', 'deepseek', 'kimi') "
        "AND config ? 'model_low'"
    )

    # ── workflows: tiers back to concrete model ids ──────────────────────────
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'high', 'claude-opus-4-8')"
    )
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'medium', 'claude-sonnet-4-6')"
    )
    op.execute(
        "UPDATE workflows SET yaml_content = replace(yaml_content, 'low', 'claude-haiku-4-5')"
    )

    # ── skills: restore haiku/sonnet/opus ────────────────────────────────────
    op.execute("ALTER TABLE skills DROP CONSTRAINT IF EXISTS ck_skills_model")
    op.execute("UPDATE skills SET model = 'opus' WHERE model = 'high'")
    op.execute("UPDATE skills SET model = 'sonnet' WHERE model = 'medium'")
    op.execute("UPDATE skills SET model = 'haiku' WHERE model = 'low'")
    op.execute("ALTER TABLE skills ALTER COLUMN model SET DEFAULT 'sonnet'")
    op.create_check_constraint(
        "ck_skills_model", "skills", "model IN ('haiku', 'sonnet', 'opus')"
    )

    # ── runs: drop integration FKs and restore NOT NULL run_branch ───────────
    op.drop_constraint("fk_runs_ai_vendor_integration_id", "runs", type_="foreignkey")
    op.drop_constraint("fk_runs_jira_integration_id", "runs", type_="foreignkey")
    op.drop_constraint("fk_runs_github_integration_id", "runs", type_="foreignkey")
    op.drop_column("runs", "ai_vendor_integration_id")
    op.drop_column("runs", "jira_integration_id")
    op.drop_column("runs", "github_integration_id")
    # NOTE: fails if any engine-initialized run has a run_branch — downgrade is
    # only valid before the first engine-owned run exists.
    op.alter_column("runs", "run_branch", existing_type=sa.Text(), nullable=False)
