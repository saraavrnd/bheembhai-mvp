# ADR-007: Project-scoped roles and request-changes routing

**Status:** Partially superseded by ADR-008 (2026-08-10) — the fixed `memberships.role` enum and
its "free-form roles rejected" rationale are replaced by an extensible `ProjectRole` catalog. The
`request_changes` routing decision below is unaffected and still stands. · **Date:** 2026-08-10 ·
**Deciders:** Saraav

## Context

EPIC BEEM-24 requires role-based access for project-level operations: who can approve at a
policy gate, who can manage workflows/policies, and who can view execution history. The
initial design used two hardcoded platform-level roles (`any` / `lead`) on the `users` table —
this is insufficient for real project governance where roles are project-scoped (a user may
be a Reviewer on Project A but a Developer on Project B).

The PRD calls for role-based approval eligibility. The workflow needs a deterministic rule
for `request_changes`: when a reviewer requests changes at a gate, where does the run go?

## Decision

**Two-tier role model: platform role + project-scoped membership roles.**

- `users.platform_role` — platform-level administration role (e.g., `PLATFORM_ADMIN`, `USER`).
  Controls platform-wide actions: adding new project roles to the catalog, managing users.

- `memberships` table — a user's membership in a project with a specific project-scoped role
  (e.g., `PROJECT_MANAGER`, `DEVELOPER`, `REVIEWER`). A user can have different roles in
  different projects. Policy gates check the user's project role when deciding if they can
  approve at a gate.

- **`request_changes` routing**: sends the run back to the **immediately previous step** in the
  workflow, creating a new attempt for that step while preserving the review record and the older
  attempt history. The previous step's prompt includes the reviewer's feedback via the existing
  hand-off mechanism (`upstream_handoff` in step context). This is deterministic and requires no
  workflow configuration changes — the engine just backtracks one step.

## Alternatives considered

- **Flat platform roles only (`any`/`lead`) (rejected):** The initial design. Cannot express
  "user A can approve on Project X but not Project Y." A single `role` column can't model
  project-specific governance.
- **Routing `request_changes` to a separate rework queue or named target (rejected):** Adds
  complexity the workflow doesn't need. The previous step is unambiguous — the engine already
  knows the step sequence.
- **Routing `request_changes` per the workflow's `on:` map (rejected):** The `on:` map for
  `changes_requested` already defines routing when a SKILL reports `changes_requested`. A
  HUMAN reviewer's `request_changes` is semantically different: the reviewer is sending work
  back for revision, not reporting a problem with their own output. Routing to the previous
  step keeps this distinct and deterministic.

## Consequences

- **Easier:** Approval checks can be enforced consistently across the UI, API, and engine
  using the user's project membership role. The engine can resolve "can this user approve at
  this gate?" by comparing `memberships.role` against the policy gate's `role` field.
- **Easier:** Platform roles (`PLATFORM_ADMIN`) are separate from project governance — a
  platform admin doesn't automatically have approval rights on every project's gates.
- **Easier:** `request_changes` has a deterministic path back into the workflow, which keeps
  the engine simple for MVP.
- **Harder:** Every gate approval check now requires a DB lookup on `memberships`. Mitigated
  by loading the membership at the start of the gated step and caching it in the run context.
- **Harder:** The `users` table must be extended with `platform_role`, and a new `memberships`
  table must be created and populated before any gated run can complete.
