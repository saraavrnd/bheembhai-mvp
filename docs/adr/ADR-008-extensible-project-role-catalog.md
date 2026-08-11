# ADR-008: Extensible project-role catalog

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

ADR-007 established project-scoped membership roles and a two-tier role model (platform +
project). The initial set of project roles was expected to be small and stable: `PROJECT_MANAGER`,
`DEVELOPER`, `DEVOPS`, `REVIEWER`.

However, the full SDLC spans more roles than these four: Business Analyst, Architect, QA, and
others. Hardcoding the role vocabulary as a DB enum or a fixed application constant means every
new role requires a schema change or code deploy — blocking the platform from scaling with
business needs. The platform needs to add new project-membership roles at runtime without a
schema change or redeploy.

This ADR amends the project-role vocabulary portion of ADR-007 (the `request_changes` routing
decision and the two-tier role model are unaffected and still stand).

## Decision

**Introduce a platform-wide `project_roles` catalog table** with a stable `key` as the primary
identifier:

| Column | Type | Purpose |
|--------|------|---------|
| `key` | TEXT PRIMARY KEY | Stable identifier (e.g., `PROJECT_MANAGER`, `QA`) |
| `label` | TEXT NOT NULL | Human-readable display name |
| `is_system_default` | BOOLEAN NOT NULL DEFAULT false | True for the 7 seed roles |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT NOW() | |

`memberships.role` becomes a plain TEXT column with a FOREIGN KEY reference to
`project_roles.key` — validated at the application layer, not by a DB enum. The JSON shape
of the membership API is unchanged (`role: string`), so the contract is stable.

**Seed the catalog** at first migration with 7 `is_system_default = true` rows:

| Key | Label |
|-----|-------|
| `PROJECT_MANAGER` | Project Manager |
| `DEVELOPER` | Developer |
| `DEVOPS` | DevOps |
| `REVIEWER` | Reviewer |
| `BUSINESS_ANALYST` | Business Analyst |
| `ARCHITECT` | Architect |
| `QA` | QA |

**New API endpoints:**

- `GET /roles` — list the full catalog. Lets any role-picker UI (project member management,
  policy gate role selector) populate dynamically instead of a hardcoded list.
- `POST /roles` — add a new role. Only users with `platform_role = PLATFORM_ADMIN` may call
  this — adding a role type is a platform-wide governance action, distinct from assigning an
  existing role to a project member.

The catalog is **platform-wide**, not per-project: every project draws from the same shared
role list. Nothing in the requirements asks for project-specific custom roles, and a shared
catalog is the simpler MVP shape. Per-project custom roles can be a later amendment if a real
need emerges.

## Alternatives considered

- **Open string validated against a config/settings list (not DB-backed):** Rejected — this is
  exactly what a fixed enum already fails at (no stable, queryable vocabulary; adding a role
  still means a config/deploy change, not true runtime extensibility).
- **Grow the hardcoded set to 7 values, defer runtime extensibility:** Rejected — the
  requirement explicitly asks for the ability to add roles at runtime, and a fixed (even larger)
  set still requires a code change per new role.
- **Per-project custom role catalogs:** Rejected for now — no story or stated requirement needs
  project-specific roles; adds scoping/ownership complexity (uniqueness per project vs. global,
  per-project role-management UI) the MVP doesn't need yet.

## Consequences

- **Easier:** Adding a new project role becomes a data operation (`POST /roles`), not a code
  change — satisfies the "scale with business needs" requirement directly.
- **Easier:** `memberships.role`'s JSON shape is unchanged — any API consumer already written
  against `role: string` doesn't need to change its contract, only its assumed value set.
- **Easier:** `project_roles` is a natural home for future role-level metadata (e.g., an
  "eligible to approve" flag, a description field) without another migration.
- **Harder:** Referential integrity depends on `project_roles` existing before a `membership`
  can reference it. The seed migration must run before any membership is created. `POST /roles`
  must be idempotent-safe against key collisions (409 Conflict on duplicate key).
- **Note:** If a genuine need for per-project custom roles emerges later, this ADR would need a
  follow-up amendment (`project_roles.project_id` nullable FK, scoping rules) rather than a
  rewrite.
