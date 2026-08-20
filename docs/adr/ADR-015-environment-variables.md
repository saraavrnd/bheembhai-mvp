# ADR-015: Environment-variable management (project + platform scope)

**Status:** Accepted · **Date:** 2026-08-20 · **Deciders:** Saraav
**Amends:** ADR-012 (secret storage — reuse), ADR-013 (init resolution §2, env bundle §5),
`CLAUDE.md` (environment variables section, guardrails)

## Context

Users need to configure environment variables that are exported into every step container:
tuning the per-run guardrail knobs (max step visits, max attempts per stage) and providing
skill-specific values such as auth keys for tools. Requirements:

- **Two value kinds** — plain text (config) and secret (auth keys). Secret values must live in
  a managed secret store (SSM) and be retrieved at runtime, never persisted in the DB.
- **Two scopes** — platform-wide variables and project-specific variables. A project variable
  with the same name as a platform variable **overrides** it.
- Stored in a table; secret rows carry a reference key to the secret store instead of a value.

Everything this needs already exists in pattern form: the pluggable **SecureStorage**
protocol + `credential_ref` (ADR-012), the **InitContext** env bundle (ADR-013 §5), the
engine-side guardrail reads, and Alembic migrations.

## Decision

**A new `environment_variables` table feeds a per-run merged env dict, resolved once at run
init, injected into every step container with engine-owned keys winning.**

### Model

Table `environment_variables`: `name`, `scope` (`platform` | `project`), `project_id`
(NULL for platform rows), `value_type` (`plain` | `secret`), `value` (plain only),
`credential_ref` (secret only), `description`, timestamps. Constraints enforce the
locksteps: platform ↔ NULL project_id, project ↔ non-NULL project_id, plain ↔ value-only,
secret ↔ ref-only; `UNIQUE(project_id, name)` (Postgres NULL-distinct → platform names are
unique too).

### Secrets (ADR-012 reuse)

Secret values are written to SecureStorage on save under
`/bheembhai/env/{project_id|platform}/{name}` — inside the existing IAM prefix — and the row
stores only the opaque ref. The **engine resolves refs fresh at run init**; the platform
never reads them back (responses show `value: null`, `has_value: true`). Renaming a secret
row migrates the stored secret (get old → put new → delete old). Deleting a row deletes the
stored secret.

### Resolution + injection

At `_init_run` (bookkeeper principle, ADR-013 §2 — same phase as integrations): query
platform + project rows for the run's project, merge platform-first so project rows win
(`shared/bheembhai/env_vars.py`), resolve every secret ref. An unresolvable secret is a
deterministic config failure → `InitFailure("failed_execution")` before any container
launches. The merged dict rides `InitContext.env_vars`; `build_env_bundle` overlays it with
`env.setdefault` — engine-owned keys win (defense in depth on top of save-time validation).

### Reserved names

Keys owned by the engine/agent (git/model/vendor/Jira/context channels, per-attempt
`BB_*_URL` presigns, `DockerRuntime.env_forward` debug knobs) are rejected at save time
(400). **Exception — the tunables:** `BB_MAX_STEP_VISITS` and `BB_MAX_ATTEMPTS` are
user-settable (validated as int ≥ 1 at save) and are consumed by the engine per-run
(`_env_int` — garbage falls back to the engine default) in addition to being exported to
the container. The step `deadline` stays workflow-YAML-owned — deliberately not tunable.

### API + UI

- Project router (`/api/projects/{id}/environment-variables`): GET = member (merged view:
  platform rows read-only with `overridden` flag, project rows with `overrides_platform`);
  POST/PATCH/DELETE = project manager. `value_type` is immutable on PATCH (422 — convert by
  delete + re-create). Duplicate name → 409 (plus IntegrityError backstop).
- Admin router (`/api/admin/environment-variables`, `require_admin`): same verbs for
  platform-scoped rows. UI: an Admin-area page manages platform vars; the project
  config tab lists platform vars read-only and edits project vars (secrets masked, Replace
  to rotate).
- Plain values are returned by GET (they are config, PMs edit them); secret values are
  never returned. **Values are never logged** — resolution logs names/refs only.

## Alternatives considered

- **Pin the secret store to AWS SSM (rejected):** the pluggable SecureStorage already
  abstracts SSM (dev) vs Secrets Manager (prod) — pinning would fork the integration path
  for no benefit.
- **Store secret values in the DB (rejected):** violates the existing credential rule —
  raw secrets never persist (ADR-012).
- **Inject via an env file mounted into the container (rejected):** reintroduces host
  mounts (ADR-014 removed them) and a second delivery channel for one data class.
- **Resolve env vars in the state machine instead of init (rejected):** resolution must
  fail fast *before* container minutes are spent — the init phase is the designed surface
  for config failures (ADR-013 §2), and a mid-run resolution change would let a
  half-configured run drift between steps.
- **Let user vars override engine keys (rejected):** a user variable named `GH_TOKEN`
  would silently replace the run's git credential — reserved-name rejection + setdefault
  keep the engine authoritative.

## Consequences

- **Easier:** one mechanism (SecureStorage) for every secret; guardrail knobs become
  per-run config without engine redeploys; the bookkeeper split is preserved (platform
  validates + stores, engine resolves + injects).
- **Easier:** project isolation falls out of the table + merge — no new authz surface
  beyond the existing member/manager/admin gates.
- **Harder:** a deleted/rotated secret behind a run's ref fails the *next* run at init —
  by design (fail-fast), surfaced as `failed_execution` with the variable name.
- **Harder:** two new CRUD surfaces (project + admin) with UI parity to maintain.
- **Doc updates required:** `CLAUDE.md` (environment variables section, guardrails),
  this ADR.
