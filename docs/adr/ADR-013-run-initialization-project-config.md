# ADR-013: Run initialization — engine-owned branch, project-config env bundle, S3 skills

**Status:** Proposed · **Date:** 2026-08-13 · **Deciders:** Saraav
**Amends:** `docs/architecture.md` (Run submission, Step execution), `docs/data-model.md` (`runs.run_branch`), `CLAUDE.md` (Git mode)

## Context

The R&D prototype hardcoded run-adjacent values on the platform side: `create_run` derived
`run_branch` itself and set `source_branch="main"` because `runs.run_branch` is `NOT NULL`
and the engine worker was still a walking skeleton. The PM self-service work surfaced this
as a divergence from the documented architecture (`architecture.md` says the browser sends
`source_branch`, the platform only validates + enqueues, and the agent creates the branch
on step 1).

All of these were acceptable stopgaps for initial R&D, but they are now first-class
**project configuration**: GitHub/Jira/AI-vendor integrations (with credentials in Secure
Storage per ADR-012), per-project workflow copies, and a skills library (`skills` +
`skill_files`) editable in the admin UI. The platform must degrade to a bookkeeper: capture
the user's input on the run, hand it to the engine, and let the engine initialize from
project configuration.

Seven decisions were locked with the owner:

1. `runs.run_branch` becomes nullable — the engine derives and persists it.
2. The branch is created at **engine initialization** (not by the agent on step 1), with a
   suffix making the name unique.
3. The run modal shows the project's **"Test connection verified"** integrations only:
   GitHub, Jira, and one AI vendor (single-select when several are enabled). Selections are
   captured on the run and handed to the engine.
4. Model tiers are **high / medium / low everywhere**. Each AI-vendor integration carries the
   mapping as flat config keys `model_high` / `model_medium` / `model_low` (tier → concrete
   vendor model id), matching the existing integration-field registry convention. The engine
   resolves tier → model at run initialization.
5. Stage-specific skills are stored in **S3**; the container downloads its skill as part of
   container initialization. AWS credentials and the S3 path are passed as env vars. This
   also solves the Fargate story (no host volumes).
6. The runtime is pluggable: local Docker for dev, Fargate for prod, behind a Runtime
   protocol.
7. Secrets are resolved per launch from Secure Storage, never persisted, with last-4
   fingerprints in diagnostics.

## Decision

### 1. Run submission — the platform is a bookkeeper

`POST /api/runs` request becomes:

```json
{
  "project_id": "…",
  "workflow_id": "…",
  "policy_id": "… | null",        // null → workflow's active policy (unchanged)
  "story_id": "LNPRTL-101",
  "github_integration_id": "…",   // required
  "jira_integration_id": "… | null",      // optional
  "ai_vendor_integration_id": "…" // required
}
```

- The modal lists only integrations with `verified_at IS NOT NULL` ("Test connection
  verified") — enforced in the UI **and** validated server-side (HTTP 422).
- `source_branch` is **not** user input: it resolves at run creation from the selected GitHub
  integration's `config.base_branch` (fallback `"main"`) so the NOT NULL column is filled;
  the engine reads the stored value at init. (Optional override deferred until asked for.)
- Platform creates the run row (`state=pending`, `run_branch=NULL`) and INSERTs a
  `work_queue` item (`action=start`, payload `{story_id}`). Everything else the engine
  reads from the DB — the run row is the source of truth for the selections.
- Migration: `runs.run_branch` → nullable; three new nullable FK columns on `runs` →
  `project_integrations.id`: `github_integration_id`, `jira_integration_id`,
  `ai_vendor_integration_id` (explicit columns, matching the existing workflow_id/policy_id
  style; JSONB rejected — FKs + auditability beat flexibility here).

### 2. Engine run initialization (`_init_run`, claimed from `work_queue`)

On claiming a `start` item, the engine executes an idempotent init sequence **before** any
task launch:

1. **Load**: run row, project, workflow YAML, policy YAML, selected integrations.
2. **Validate**: GitHub + AI-vendor integrations present; workflow↔policy pairing; workflow
   steps reference skills that exist in the `skills` table (already validated at
   workflow-save time, re-checked here). Jira selection is optional at MVP — a skill that
   needs Jira fails on its own diagnostics if none was selected (follow-up: skills declare
   `requires:` in SKILL.md metadata and init validates against it).
3. **Derive branch name**: `feat/<safe_story>/<DDMMYYYYHHmm>-<first-4-of-run-uuid>`.
4. **Create the branch via the GitHub REST API** (`POST /repos/{owner}/{repo}/git/refs`,
   `refs/heads/<run_branch>`, sha = the source branch HEAD) using the `GH_TOKEN` resolved
   from the integration's `credential_ref` via Secure Storage (ADR-012). No git binary and
   no clone on the engine — the engine is a state machine, not a git client.
   - **Idempotency** (crash re-claim per ADR-003 recovery): if the ref already exists with
     the same SHA → proceed (init already happened); exists with a different SHA → suffix
     bump and retry once.
5. **Resolve models**: for each workflow step, map its `model:` tier (high/medium/low)
   through the selected AI-vendor integration's flat config keys (`model_high`,
   `model_medium`, `model_low`) to a concrete model id. Missing tier mapping → init fails
   with a clear error (no containers launched). `BB_ALLOWED_MODELS` for the run = those
   three values.
6. **Pin skill versions**: record the S3 object key (see §3) for each step's skill on the
   step row — the run executes the skills as of init time, immune to mid-run edits.
7. **Persist**: `run_branch`, `state=running`, step rows (all pending), a `transitions`
   audit row per action ("branch created", "models resolved", …).
8. **Launch step 1** through the Runtime protocol (§4).

Init failure classification: git/auth failures → `failed_infra`/`failed_execution` on the
run with the reason in `transitions`; the work item goes to `done`. Retry semantics match
the engine's existing retry family.

**Tier migration (high/medium/low everywhere):**

- `workflows.yaml_content`: data migration on existing rows —
  `claude-opus-4-8 → high`, `claude-sonnet-4-6 → medium`, `claude-haiku-4-5 → low`
  (template + project copies). Workflow save validation accepts only `high|medium|low`.
- `skills.model`: migrate `{opus → high, sonnet → medium, haiku → low}` and replace the
  `ck_skills_model` check constraint (the column remains "default tier hint for the skill").
- AI-vendor integration config carries the flat keys `model_high` / `model_medium` /
  `model_low` (previously `model_small` — data-migrated and renamed in the registry).
  Validated at integration save: all three keys present and non-empty for AI-vendor types.
  Shown read-only in the run modal next to the vendor (H/M/L summary).
- Model-profile indirection (a `model_profiles` table) is explicitly deferred; the mapping
  lives on the integration for MVP.
- The on-disk `config/*.yaml` seed source is updated to the tier vocabulary too — otherwise
  `seed_default_workflows` re-imports the old concrete model ids on every startup and
  silently reverts the data migration on template rows.

### 3. Skills delivered from S3 (Object Storage, ADR-011)

- **Publish**: the `skills` + `skill_files` DB rows remain the source of truth. On skill
  save, the platform writes the files to Object Storage under
  `skills/<name>/<content-hash>/` (directory layout `SKILL.md`, `references/`, `templates/`,
  `examples/`).
- **Pin**: engine init records the per-step S3 key (name + hash) on the step row.
- **Consume**: the agent image no longer bakes skills. At container start, `run_skill.sh`
  downloads the skill into `/skills/<name>` before invoking Claude Code. Env passed by the
  engine: `SKILL_S3_BUCKET`, `SKILL_S3_KEY`, and AWS credentials (access key / secret /
  region) — per decision, credentials travel as env vars; this works identically for local
  Docker and Fargate.
  - Security note: `presigned_get_url` (already in the storage protocol) would avoid
    spreading AWS credentials into task envs, and a Fargate task IAM role is the
    production-grade variant. Both are drop-in later; MVP uses env creds per decision.

### 4. Runtime protocol (pluggable)

Same pattern as the R&D engine: `Runtime` protocol with `launch / status / reconcile`.
- `DockerRuntime` — local dev (docker-compose engine), mirrors the R&D engine.
- `FargateRuntime` — boto3 `run_task`/`describe_tasks`, per the architecture docs.
Selected by engine config (`BB_RUNTIME=docker|fargate`).

### 5. Env bundle per launch (project config → container env)

Composed by the engine at each step launch, sourced from the run's captured selections:

| Group | Env vars | Source |
|---|---|---|
| Git | `BB_GIT_MODE=1`, `GIT_REMOTE_URL`, `GIT_SOURCE_BRANCH` (= default_branch), `RUN_BRANCH`, `GH_TOKEN` | GitHub integration config + `credential_ref` via Secure Storage |
| Jira | `JIRA_URL`, `JIRA_USERNAME`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_USER_EFFECTIVE` | Jira integration config + `credential_ref` (absent if not selected) |
| Model | `BB_MODEL` (tier-resolved), vendor API key, `ANTHROPIC_BASE_URL` for non-Anthropic vendors, `BB_ALLOWED_MODELS` | AI-vendor integration flat tier keys (`model_high/medium/low`) + `credential_ref` |
| Engine | `RUN_ID`, `STEP_ID`, `ATTEMPT_NO`, `SKILL`, `RESULT_DIR`, `BB_CONTEXT`/`CONTEXT_FILE`, `STORY_ID` | Engine state + context injection |
| Skills | `SKILL_S3_BUCKET`, `SKILL_S3_KEY`, AWS creds | Object Storage config + pinned skill key |

Hygiene: secrets resolved fresh at each launch (never persisted, never in `transitions`);
last-4 fingerprints in diagnostics; MCP config substitution remains in the agent (`mcp.json`
template).

## Alternatives considered

- **Platform-side branch naming (rejected):** the current stopgap. Diverges from the
  documented architecture and hardcodes what is now project configuration.
- **Agent creates the branch on step 1 (rejected):** per `architecture.md`, but fails late —
  a bad GitHub token or missing ref costs a Fargate launch before surfacing. Engine init
  fails fast before any container starts. The doc is amended accordingly.
- **`git` binary + clone on the engine (rejected):** heavy, needs repo files the engine
  never reads, and complicates crash idempotency. The GitHub API ref-creation is a single
  idempotent call with the token we already resolve.
- **Skills baked into the agent image (rejected):** skill edits require an image rebuild and
  per-project skill variants are impossible. S3 delivery works for local Docker and Fargate
  with one mechanism.
- **`model_profiles` table for tier indirection (deferred):** useful for platform-wide tier
  governance, but the per-vendor `model_map` on the integration covers MVP needs without new
  admin UI or schema.
- **JSONB for run integration selections (rejected):** explicit FK columns give referential
  integrity and match the existing workflow_id/policy_id convention.

## Consequences

- **Easier:** Platform `create_run` shrinks to validation + row + enqueue — the stopgap
  naming code is deleted.
- **Easier:** Engine init is a single idempotent sequence; crash recovery (ADR-003) re-claims
  stale `start` items safely because branch creation and model resolution are repeatable.
- **Easier:** Fail-fast: bad git credentials, missing tier mapping, or missing integrations
  surface at init as classified run failures — before any container minutes are spent.
- **Easier:** Skill updates flow DB → S3 → next run, no image rebuilds; Fargate has no host
  volumes to manage.
- **Harder:** The agent container now needs the skills-download phase and its env contract
  grows (`SKILL_S3_*`, AWS creds). R&D image (skills baked) must be updated to match.
- **Harder:** A data migration rewrites workflow YAML `model:` values in place; project
  copies edited by PMs need the same tier vocabulary from day one (validation error
  otherwise).
- **Harder:** Run modal gains integration selection UI with the verified-only filter; runs
  created before this change have NULL selections and NULL `run_branch` (UI must render both
  gracefully).
- **Doc updates required:** `architecture.md` (run submission + step execution),
  `data-model.md` (`runs` columns, `work_queue` payload), `CLAUDE.md` (git mode, skills).
