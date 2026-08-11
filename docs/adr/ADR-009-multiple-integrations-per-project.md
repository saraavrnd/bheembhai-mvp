# ADR-009: Multiple integrations per project (same type)

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

The initial data model constrained a project to one integration per type via
`UNIQUE (project_id, type)` — one GitHub and one Jira connection. This was based on an
assumption that a project maps to a single code repository and a single Jira project.

A real project can have multiple Jira boards (e.g., backend team board, frontend team board)
and multiple GitHub repositories (e.g., main service repo, documentation repo, shared-library
repo). A run should be able to target any of the project's configured integrations when it
clones code or fetches story details.

## Decision

**Remove the per-type limit. Projects can have multiple integrations of the same type.**

- Add a `label` column to `project_integrations` — a human-readable name to distinguish
  integrations of the same type (e.g., "Backend Board" vs "Frontend Board", "main-repo" vs
  "docs-repo").
- Change the UNIQUE constraint from `(project_id, type)` to `(project_id, type, label)`.
  A project can add any number of integrations; each must have a distinct label within its type.
- The `config` JSONB field already carries type-specific details (`repo_url` for GitHub,
  `jira_url` + `project_key` for Jira). No structural change to `config`.
- Each integration gets its own Secrets Manager secret — credentials are not shared across
  integrations even of the same type (different Jira instances may have different API tokens).

**How runs select an integration:** at run submission time, the run request can optionally
specify which integrations to use. If omitted, defaults are inferred (first verified GitHub
integration → source repo; first verified Jira integration → story provider). This is
resolved by the Platform API and passed to the Engine as part of the run context.

## Alternatives considered

- **Keep the one-per-type limit, tell users to create separate projects (rejected):**
  Forces artificial project splits. A single product team working across two repos and one
  Jira board shouldn't need three "projects" in BheemBhai.
- **Allow multiple but without labels, identified by index or config inspection (rejected):**
  The UI and run-submission form need a stable, human-readable way to pick an integration.
  A label is simpler and more user-friendly than "GitHub #1" / "GitHub #2" or parsing URLs.
- **Unlimited with no unique constraint at all (rejected):** Without the `(project_id, type,
  label)` unique, a user could accidentally create duplicate integrations with the same label
  and not understand why both appear. The unique constraint catches this at save time.

## Consequences

- **Easier:** A project can model its real tool landscape — multiple repos, multiple Jira
  boards — without creating artificial project boundaries.
- **Easier:** The `label` field gives the UI a natural display name for pickers (run
  submission form, integration list).
- **Harder:** Run submission must now handle integration selection. The UI needs a picker
  when multiple integrations of a type exist; the API must validate that the selected
  integration belongs to the project. Mitigated by sensible defaults (use the first verified
  integration if only one exists).
- **Harder:** The Engine's git clone step must receive the specific integration's
  credentials (via Secrets Manager ARN) rather than assuming one per project. The run
  context already carries per-step configuration — this is an additional field, not a
  structural change.
