# ADR-013: Run initialization — engine-owned branch, project-config env bundle, S3 skills

**Status:** Accepted · **Date:** 2026-08-14 · **Deciders:** Saraav
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
5. Stage-specific skills are stored in **S3** as content-addressed bundles; the container
   downloads its skill via a fresh presigned URL as part of container initialization —
   no AWS credentials in agent envs, no host volumes (solves the Fargate story too).
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
- `source_branch` resolves at run creation from an optional per-run override in the run
  modal (the branch the engine cuts the run branch off), falling back to the selected GitHub
  integration's `config.base_branch` (then `"main"`) so the NOT NULL column is filled. The
  platform validates the override with git check-ref-format essentials (HTTP 422) and the
  engine reads the stored value at init — the run row wins over the live integration config.
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
   step row — the run executes the skills as of init time, immune to mid-run edits. A
   referenced skill with no exported bundle is packed and published from its DB files on
   the spot (self-heal) so pre-Phase-1 catalogs just work.
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

- **Publish**: the `skills` + `skill_files` DB rows remain the source of truth. On every
  skill-save that touches content (create, file add/edit/delete, import, clone-on-map),
  the platform packs the files into a **deterministic** tar.gz (entries
  `<name>/SKILL.md`, `<name>/references/…` sorted by path, mtime=0, fixed mode/owner,
  gzip header mtime=0) and PUTs it to Object Storage under the **content-addressed key**
  `skills/<name>/<sha256>.tar.gz`; key + sha are stamped on the skill row. Re-packing the
  same content yields the same key, so concurrent publishes are idempotent (head-check
  skips the re-PUT). Metadata-only edits (description/model) do not re-publish, and skill
  delete keeps the object (in-flight determinism; bundle GC is out of scope).
- **Pin**: engine init records the per-step S3 key + sha on the step row — first init
  stamps it, later dispatches read the persisted row (never a re-resolved map), and NULL
  pins on pre-migration in-flight runs are backfilled. A referenced skill with no exported
  bundle is packed and published from its DB files at init (self-heal).
- **Consume**: the agent image is a pure runtime — no skills baked. At container start,
  `run_skill.sh` downloads the skill bundle via env `BB_SKILL_URL` (a **fresh presigned
  GET**, `expires_in=900`, signed by the engine at launch from the pinned key) and
  `BB_SKILL_SHA256` (verification), then extracts it into `.claude/skills/<name>` —
  unconditionally overwriting anything the repo tracks there (BheemBhai is authoritative).
  This is the security-preferred variant of the original decision: AWS credentials never
  travel into agent envs (a Fargate task IAM role remains an alternative later).

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
| Skills | `BB_SKILL_URL` (fresh presigned GET, 900s), `BB_SKILL_SHA256` | Pinned step-row skill key + Object Storage presigner |

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

## Implementation notes (BEEM-24, 2026-08-14)

Implemented in the Engine Service (`engine_service/`): decisions §1–§2 (bookkeeper run
submission, `_init_run` with idempotent GitHub REST branch creation + failure
classification), §4 (Runtime protocol — `DockerRuntime`), §5 (env bundle with the vendor key
rule: `claude` → `ANTHROPIC_API_KEY`, others → `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`),
and §6–§7 (tier model resolution from integration config, secrets resolved per launch).

Two behaviours settled at implementation time (supersede this ADR where they differ):

- **Gate pause = `runs.state = "paused"`.** The run-level state machine is
  `pending → running ⇄ paused → completed|failed`. The gated step row keeps
  `exec_state = "completed"` (the UI renders `is_awaiting_review` from that); the gate card
  is persisted in the `awaiting_approval` **transition's** JSONB payload so it survives
  restarts.
- **Gate decisions are queued, not applied.** `POST /api/runs/{id}/decision` validates
  `run.state == "paused"` (else 409) and INSERTs a `work_queue` item
  (`action="continue"`, payload `{action: approve|send_back, send_back_to, comment, actor}`)
  with **no state mutation**. The engine claims the token and applies the decision; the UI
  re-poll sees the flip.

**Phase 1 implemented (2026-08-19):** §3 S3 skill delivery — content-addressed deterministic
bundles (`skills/<name>/<sha256>.tar.gz`, packed on every content-changing save in the
platform, self-heal pack at engine init for unexported skills), step-row pinning at first
init with NULL backfill for pre-migration in-flight runs, a fresh presigned GET per launch
into `BB_SKILL_URL` + `BB_SKILL_SHA256` env (no AWS creds in agent envs — the ADR's own
security-preferred variant), a pure-runtime agent image (no baked skills), and
`run_skill.sh` download → sha256 verify → path-safety check → extract into
`.claude/skills/<name>`. Context travels via the `BB_CONTEXT` env var written by the runner
to `/home/node/context.json` — the `/ctx` bind mount is gone.

**Deferred** (unchanged, no new decisions): `FargateRuntime` (DockerRuntime is the
local-dev runtime; `steps.fargate_task_arn` is reused as the generic runtime handle),
`model_profiles` table, budget caps.

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
  grows (`BB_SKILL_URL`, `BB_SKILL_SHA256`). Every skill consumed by a run must have an
  exported bundle first (publish-on-save + engine self-heal make this automatic).
- **Harder:** Compose runs the real S3 backend (`STORAGE_BACKEND=s3` + AWS creds). The
  `local` backend's `file://` URLs are host paths that agent containers cannot resolve,
  so `local` remains valid only for host-side runs (unit tests).
- **Harder:** A data migration rewrites workflow YAML `model:` values in place; project
  copies edited by PMs need the same tier vocabulary from day one (validation error
  otherwise).
- **Harder:** Run modal gains integration selection UI with the verified-only filter; runs
  created before this change have NULL selections and NULL `run_branch` (UI must render both
  gracefully).
- **Doc updates required:** `architecture.md` (run submission + step execution),
  `data-model.md` (`runs` columns, `work_queue` payload), `CLAUDE.md` (git mode, skills).
