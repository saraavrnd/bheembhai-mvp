# ADR-006: Policy must be tied to a specific workflow

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

The current policy YAML files declare `applies_to: story-delivery` — a loose string match
against a workflow name. This is fragile: if two projects each have a workflow named
`story-delivery`, there's no way to distinguish which one the policy gates. More critically,
the engine's `validate_pairing()` function must verify that every gate in the policy targets
a step that exists in the workflow, and that every status the policy gates on has a valid
routing target in the workflow's `on:` map. Without a formal foreign-key relationship,
this validation is best-effort.

EPIC BEEM-24 requires policy CRUD. Users will create and edit policies. Without a hard
foreign-key constraint, a policy could reference a deleted workflow or gate on a status the
workflow can't route from — producing a run-time error deep in the engine instead of a
save-time validation error the user can act on.

## Decision

**Every policy row has `workflow_id` (FK to workflows, NOT NULL).** A policy belongs to
exactly one workflow. The workflow already belongs to a project (`workflow.project_id`), so
the policy is transitively scoped to the same project.

At policy save time (Platform API), the validator loads the referenced workflow's step
definitions and `on:` routing maps, and rejects the policy if:
- Any gated step doesn't exist in the workflow
- Any gated status (explicit `on_status: [...]`) has no matching routing target in the
  workflow's `on:` map for that step

This is the same `validate_pairing()` logic the engine already runs — it just moves to
save-time validation so the user gets immediate feedback rather than a run-time failure.

## Alternatives considered

- **Loose name reference `applies_to` (rejected):** What the YAML currently does. Simple but
  doesn't scale past one workflow per project. No referential integrity — deleting a workflow
  leaves orphaned policy references.
- **Policy belongs to project, workflow reference is optional (rejected):** More flexible
  (one policy could hypothetically apply to multiple workflows), but the validation gets
  harder (which workflow's steps do you validate against?) and the UX gets confusing (which
  workflow does this policy gate?).

## Consequences

- **Easier:** Referential integrity — deleting a workflow cascades to or blocks on its
  policies. Save-time validation gives immediate, clear errors.
- **Easier:** The UI can show "Policies for this workflow" as a natural parent-child
  relationship.
- **Harder:** A policy cannot be reused across workflows. If two workflows share the same
  step structure and the user wants the same gating, they must create two policies (or
  clone one). This is acceptable for MVP — policy reuse across workflows is a rare need
  compared to correctness guarantees.
- **Harder:** The `validate_pairing()` logic must be available at policy save time (Platform
  API) AND at run submission time (Engine). Currently it lives in the engine. It must be
  extracted to `backend/shared/` so both services can use it without duplicating code.
