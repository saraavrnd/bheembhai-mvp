# Data Model — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10 · **Database:** PostgreSQL 16 (RDS)

## Entity-Relationship Diagram

```
users 1───* projects            (owner_id)
users 1───* memberships         (user's project roles)
users 1───* runs                (started_by_user_id — who submitted the run)
projects 1───* memberships      (project members)
project_roles 1───* memberships (role key)
projects 1───* project_integrations
project_integrations 1───* runs (github/jira/ai_vendor selections, ADR-013 §1)
projects 1───* workflows
workflows 1───* policies        ← policy tied to one workflow (ADR-006)
projects 1───* runs
workflows 1───* runs
policies 1───* runs
runs 1───* steps
steps 1───* transitions
runs 1───* work_queue
```

## Tables

### users

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `external_id` | TEXT | NOT NULL | Provider-scoped stable user identity (Cognito `sub`, Azure `oid`, Okta `sub`). Part of UNIQUE (external_id, auth_provider). ADR-010. |
| `auth_provider` | TEXT | NOT NULL | The provider that authenticated this user (e.g. `"cognito"`, `"azure_ad"`, `"okta"`). Part of UNIQUE (external_id, auth_provider). ADR-010. |
| `email` | TEXT | NOT NULL | From the provider's `email` claim |
| `display_name` | TEXT | NOT NULL | From the provider's `name` claim or email prefix |
| `platform_role` | TEXT | NOT NULL, DEFAULT 'USER', CHECK (platform_role IN ('PLATFORM_ADMIN', 'USER')) | Platform-level administration role. Only PLATFORM_ADMIN can add new project roles to the catalog (ADR-008). |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

### project_roles

ADR-008: Extensible catalog of project-level roles. Platform-wide — every project draws from
the same list. Seed migration creates 7 system defaults.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `key` | TEXT | PK | Stable identifier (e.g., `PROJECT_MANAGER`, `QA`) |
| `label` | TEXT | NOT NULL | Human-readable display name |
| `is_system_default` | BOOLEAN | NOT NULL, DEFAULT false | True for the 7 seed roles |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

Seed roles (first migration):

| Key | Label | is_system_default |
|-----|-------|-------------------|
| `PROJECT_MANAGER` | Project Manager | true |
| `DEVELOPER` | Developer | true |
| `DEVOPS` | DevOps | true |
| `REVIEWER` | Reviewer | true |
| `BUSINESS_ANALYST` | Business Analyst | true |
| `ARCHITECT` | Architect | true |
| `QA` | QA | true |

### memberships

ADR-007: A user's membership in a project with a specific project-scoped role. A user can
have different roles in different projects. Policy gate approval checks resolve against the
user's membership role for the run's project.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `user_id` | UUID | FK → users.id ON DELETE CASCADE, NOT NULL | |
| `project_id` | UUID | FK → projects.id ON DELETE CASCADE, NOT NULL | |
| `role` | TEXT | FK → project_roles.key, NOT NULL | Project-scoped role from the catalog |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

UNIQUE: `(user_id, project_id)` — one membership per user per project (one role at a time).

### projects

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `name` | TEXT | NOT NULL | |
| `owner_id` | UUID | FK → users.id, NOT NULL | Creator/owner |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

### project_integrations

ADR-009: A project can have multiple integrations of the same type (e.g., two Jira instances,
multiple GitHub repos). Each integration has a `label` to distinguish it. Credentials live in
the DB stores only the opaque `credential_ref`.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `project_id` | UUID | FK → projects.id ON DELETE CASCADE, NOT NULL | |
| `type` | TEXT | NOT NULL, CHECK (type IN ('github', 'jira', 'openai', 'claude', 'deepseek', 'kimi')) | Tool integrations (github, jira) + AI-vendor integrations (ADR-013 §4 model tiers) |
| `label` | TEXT | NOT NULL | Human-readable label to distinguish multiple integrations of the same type (e.g., "Backend Board", "Frontend Repo") |
| `credential_ref` | TEXT | NOT NULL | Opaque, provider-specific reference to stored credentials (Secrets Manager ARN, Key Vault URL, Vault path). The raw token is never in the DB. ADR-012. |
| `config` | JSONB | NOT NULL, DEFAULT '{}' | Integration-specific — see "JSONB field shapes" below. GitHub: `{url, repository, base_branch}`; Jira: `{jira_url, project_key}`; AI vendors: `{base_url, model_high, model_medium, model_low}` |
| `verified_at` | TIMESTAMPTZ | | NULL until the project owner verifies the integration works ("Test connection verified" — required for run selection, ADR-013 §1) |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

UNIQUE: `(project_id, type, label)` — a project can have multiple integrations of the same type
as long as each has a distinct label. No arbitrary cap on integrations per project.

### workflows

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `project_id` | UUID | FK → projects.id ON DELETE CASCADE, NOT NULL | |
| `version` | INTEGER | NOT NULL, DEFAULT 1 | Incremented on each edit |
| `name` | TEXT | NOT NULL | Human-readable label |
| `yaml_content` | TEXT | NOT NULL | The workflow YAML as stored text |
| `is_active` | BOOLEAN | NOT NULL, DEFAULT false | Only one active version per workflow name per project |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

UNIQUE: `(project_id, name, version)` — versioned. The active version is the one with
`is_active = true`. On activation, deactivate the previously active version for that name.

### policies

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `project_id` | UUID | FK → projects.id ON DELETE CASCADE, NOT NULL | |
| `workflow_id` | UUID | FK → workflows.id, NOT NULL | ADR-006: policy is tied to one workflow |
| `version` | INTEGER | NOT NULL, DEFAULT 1 | Incremented on each edit |
| `name` | TEXT | NOT NULL | Human-readable label (e.g. "Strict review") |
| `yaml_content` | TEXT | NOT NULL | The policy YAML as stored text |
| `is_active` | BOOLEAN | NOT NULL, DEFAULT false | Only one active version per policy name per project |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

UNIQUE: `(project_id, name, version)`.

**Save-time validation:** Before inserting/updating a policy, validate that every gated step
exists in the referenced workflow's `yaml_content`, and every gated status (from `on_status`)
has a matching routing target in the workflow's `on:` map. If validation fails, reject with
HTTP 422 + specific error message.

### runs

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `project_id` | UUID | FK → projects.id, NOT NULL | Scopes the run to a project |
| `workflow_id` | UUID | FK → workflows.id, NOT NULL | The workflow version used |
| `policy_id` | UUID | FK → policies.id, NOT NULL | The policy version used (must reference workflow_id) |
| `story_id` | TEXT | | Jira story key (e.g. BEEM-24), optional |
| `source_branch` | TEXT | NOT NULL | Git branch the run branch is cut from — NOT user input: resolved at run creation from the selected GitHub integration's `config.base_branch` (fallback `"main"`, ADR-013 §1) |
| `run_branch` | TEXT | NULLABLE | `feat/<story>/<DDMMYYYYHHmm>-<first-4-of-run-uuid>` — **engine-owned**: derived and persisted by the engine at `_init_run` (ADR-013 §2); NULL from creation until init |
| `github_integration_id` | UUID | FK → project_integrations.id ON DELETE SET NULL | The GitHub integration selected at submission (required, ADR-013 §1) |
| `jira_integration_id` | UUID | FK → project_integrations.id ON DELETE SET NULL | Optional Jira selection (MVP — a skill that needs Jira fails its own diagnostics if none was selected) |
| `ai_vendor_integration_id` | UUID | FK → project_integrations.id ON DELETE SET NULL | The AI-vendor integration selected at submission (required) — carries the tier → model mapping |
| `state` | TEXT | NOT NULL, DEFAULT 'pending' | Run-level state: `pending → running ⇄ paused → completed | failed`. No `awaiting_approval`/`retrying` run states — gate pause IS `paused` (gated step row stays `exec_state="completed"`); transient retries loop inside `running` |
| `current_step` | TEXT | | The step_id currently executing (or gated on, when `paused`) |
| `started_by_user_id` | UUID | FK → users.id ON DELETE SET NULL | Who submitted the run — set at creation; SET NULL keeps history if the user is deleted |
| `cost_usd` | NUMERIC(10,4) | NOT NULL, DEFAULT 0 | Cumulative cost across all steps |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

### steps

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `run_id` | UUID | FK → runs.id ON DELETE CASCADE, NOT NULL | |
| `step_id` | TEXT | NOT NULL | Matches the workflow step `id` (e.g. "story-design") |
| `skill` | TEXT | NOT NULL | The skill invoked (e.g. "story-design") |
| `exec_state` | TEXT | NOT NULL, DEFAULT 'pending' | Step-level state (pending/running/awaiting_result/completed/failed) — no `retrying`: transient retries re-run the same row with `attempt_no` incremented |
| `result_status` | TEXT | | The classified outcome from the result status enum |
| `model_requested` | TEXT | | The **concrete model id** resolved at engine init from the workflow step's tier (high/medium/low) through the selected AI-vendor integration's `model_high/medium/low` config (ADR-013 §2). Set on the row before the step first runs. |
| `models_used` | TEXT | | Comma-separated models actually used (from Claude Code output) |
| `cost_usd` | NUMERIC(10,4) | NOT NULL, DEFAULT 0 | |
| `attempt_no` | INTEGER | NOT NULL, DEFAULT 1 | |
| `fargate_task_arn` | TEXT | | **Generic runtime handle** (reused column, ADR-013 §4): container id under `DockerRuntime`; task ARN once `FargateRuntime` lands. Crash recovery re-attaches via this. |
| `artifact_storage_key` | TEXT | | Opaque storage key prefix for this step's artifacts (e.g. `runs/<run_id>/<step_id>/<attempt_no>/`). The backend prefix (S3 bucket, Azure container, local path) is provider config per ADR-011. |
| `started_at` | TIMESTAMPTZ | | |
| `ended_at` | TIMESTAMPTZ | | |

**Upsert semantics (ADR-013 §2):** ALL step rows are inserted once at engine init, each
`pending` with `model_requested` already resolved — the run executes the workflow as of init
time. Replays never re-insert rows:
- **Retry:** same row, `exec_state` back to `pending`/`running`, `attempt_no` incremented.
- **Crash re-claim (idempotent init):** rows exist → skip insertion; resume from
  persisted `exec_state`/`attempt_no`.
- **`send_back`:** rows after the named target (plus the target itself when it is the
  gated step) are reset to `pending` with `attempt_no` back to 1; rows before it keep
  their completed history.

### transitions

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | BIGSERIAL | PK | |
| `run_id` | UUID | FK → runs.id, NOT NULL | |
| `step_id` | TEXT | NOT NULL | Step-scoped rows use the step `step_id`; **run-level rows use the sentinel `''`** |
| `attempt_no` | INTEGER | NOT NULL | Step-scoped rows use the step's attempt; **run-level rows use `0`** |
| `from_state` | TEXT | NOT NULL | |
| `to_state` | TEXT | NOT NULL | |
| `result_status` | TEXT | | The result status that triggered this transition |
| `actor` | TEXT | NOT NULL, DEFAULT 'system' | 'system' or user email (for approval decisions) |
| `reason` | TEXT | | Human-readable reason for the transition |
| `payload` | JSONB | NULLABLE | Structured detail that survives restarts (ADR-003 durability): step outcomes (summary/artifacts/files) on completion rows, and the **gate card** on `awaiting_approval` rows — the engine rebuilds routing + re-notification from these after a crash. UI contract unchanged (renders from this same payload). |
| `ts` | REAL | NOT NULL | Unix timestamp |

This table is an append-only audit log — every state change is recorded. Run-level
transitions (init steps, gate decisions, run completion/failure) are distinguished by the
`step_id = ''` / `attempt_no = 0` sentinels; `awaiting_approval` lives **only here** — the
run-level state for a gated run is `paused`.

### work_queue

ADR-003: Postgres-backed FIFO work queue. The Platform API INSERTs work items; Engine
processes claim them via `SELECT ... FOR UPDATE SKIP LOCKED`. Claim + heartbeat pattern
ensures crash recovery — stale claims are re-enqueued on Engine restart.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | BIGSERIAL | PK | |
| `run_id` | UUID | FK → runs.id, NOT NULL | The run to start or continue |
| `action` | TEXT | NOT NULL, CHECK (action IN ('start', 'continue')) | `start` = new run (engine inits); `continue` = dispatch token that advances a run from `paused` (gate decision) or resumes it after a crash |
| `payload` | JSONB | NOT NULL, DEFAULT '{}' | For `start`: `{story_id}` — everything else the engine reads from the run row. For `continue`: `{action: approve|send_back|resume, send_back_to, comment, actor}` (see shapes below) |
| `state` | TEXT | NOT NULL, DEFAULT 'pending', CHECK (state IN ('pending', 'claimed', 'done')) | `pending` = unclaimed; `claimed` = being processed; `done` = run reached its next pause (gate or terminal) — one dispatch = one pause advanced |
| `claimed_by` | TEXT | | Engine instance ID (hostname or `ENGINE_ID` env var) |
| `claimed_at` | TIMESTAMPTZ | | When the Engine claimed this item |
| `heartbeat_at` | TIMESTAMPTZ | | Last liveness ping (updated every 30s by the Engine's heartbeat task). Stale (>60s) = Engine died — recovered on restart. |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

## Indexes

```sql
-- Query: "all runs for a project, newest first"
CREATE INDEX idx_runs_project_created ON runs (project_id, created_at DESC);

-- Query: "all steps for a run, in order"
CREATE INDEX idx_steps_run ON steps (run_id, step_id, attempt_no);

-- Query: "transitions for a run step"
CREATE INDEX idx_transitions_run_step ON transitions (run_id, step_id, attempt_no);

-- Query: "in-flight runs on Engine restart" (recovery step 2, ADR-003)
CREATE INDEX idx_runs_state ON runs (state) WHERE state IN ('running', 'paused');

-- Query: "active workflow for a project"
CREATE INDEX idx_workflows_active ON workflows (project_id, name) WHERE is_active;

-- Query: "active policy for a workflow"
CREATE INDEX idx_policies_active ON policies (workflow_id, name) WHERE is_active;

-- Lookup: provider + external_id → user
CREATE UNIQUE INDEX idx_users_external_id_provider ON users (external_id, auth_provider);

-- Lookup: user's memberships (for gate approval checks)
CREATE INDEX idx_memberships_user ON memberships (user_id);

-- Lookup: project members (for project detail page)
CREATE INDEX idx_memberships_project ON memberships (project_id);

-- Work queue: fast pending scan (for Engine worker loop)
CREATE INDEX idx_work_queue_pending ON work_queue (created_at) WHERE state = 'pending';

-- Work queue: stale heartbeat detection (for crash recovery)
CREATE INDEX idx_work_queue_claimed ON work_queue (heartbeat_at) WHERE state = 'claimed';
```

## JSONB field shapes

### project_integrations.config (GitHub)
```json
{
  "url": "https://github.com",
  "username": "your-username",
  "repository": "owner/repo",
  "base_branch": "main"
}
```
`base_branch` is the run's `source_branch` source (ADR-013 §1 — not user input).
Secret fields (`access_token`) are stored in Secure Storage via `credential_ref`, never in `config`.

### project_integrations.config (Jira)
```json
{
  "jira_url": "https://company.atlassian.net",
  "project_key": "BEEM",
  "default_issue_type": "Task"
}
```

### project_integrations.config (AI vendors — openai, claude, deepseek, kimi)
```json
{
  "base_url": "https://api.openai.com/v1",
  "model_high": "gpt-5",
  "model_medium": "gpt-5-mini",
  "model_low": "gpt-5-nano"
}
```
The three flat tier keys map workflow step `model:` tiers (high/medium/low) to concrete
vendor model ids — the engine resolves them at `_init_run` (ADR-013 §2/§4). All three keys
are required and non-empty, validated at integration save. `api_token` lives in Secure
Storage via `credential_ref`.

### work_queue.payload (start)
```json
{ "story_id": "BEEM-24" }
```
Everything else (integrations, workflow, policy, source branch) lives on the run row.

### work_queue.payload (continue)
```json
{ "action": "approve", "comment": "lgtm", "actor": "reviewer@example.com" }
```
```json
{ "action": "send_back", "send_back_to": "implement", "comment": "rework", "actor": "reviewer@example.com" }
```
```json
{ "action": "resume" }
```
`approve` routes via the workflow `on:` map; `send_back` rewinds to the named target
(ADR-007); `resume` is the crash-recovery token (re-attach/relaunch from persisted state).

## Migrations from existing SQLite

The existing schema maps 1:1 to Postgres with minimal changes:

| SQLite | Postgres |
|--------|----------|
| `TEXT PRIMARY KEY` for run/step IDs | `UUID PRIMARY KEY DEFAULT gen_random_uuid()` |
| `REAL` for cost | `NUMERIC(10,4)` for exact money |
| `REAL` for timestamps | `TIMESTAMPTZ` |
| No FKs | Foreign keys with CASCADE where appropriate |
| No JSONB | `config` and `result` fields as JSONB |

Test suite (BEEM-24 engine implementation): pure-logic unit tests under
`tests/unit/` (no DB) and integration tests under `tests/integration/` against the
docker-compose PostgreSQL (`postgresql+asyncpg://bheembhai-mvp:...@localhost:5555/bheembhai_test`)
with a scripted `FakeRuntime` behind the Runtime protocol — JSONB rules out SQLite for the
integration layer. Run with `pytest -m unit` / `pytest -m integration`.

## Traceability

| BEEM-24 Feature | Tables |
|-----------------|--------|
| AWS Cognito auth | `users` (external_id, auth_provider). AuthProvider protocol abstracts the provider; Cognito is the first implementation (ADR-010). |
| Users | `users`, `memberships` (project-scoped roles) |
| Projects | `projects`, `project_integrations`, `memberships` |
| Project roles | `project_roles` (catalog), `memberships` (assignment) — ADR-007, ADR-008 |
| GitHub/Jira/AI-vendor integrations | `project_integrations` (credential_ref → SecureStorage; AI vendors carry `model_high/medium/low` tier config). Pluggable protocol (ADR-012) — Secrets Manager is the first backend. |
| Workflow management | `workflows` (versioned YAML, is_active) |
| Policy management | `policies` (versioned YAML, is_active, FK → workflows) |
| Executions — history | `runs` (per project, paginated), `steps`, `transitions` |
| Executions — detail | Result volume host-mount under `DockerRuntime` (diagnostics, `bb_step_result.json`); `steps.artifact_storage_key` is the deferred FargateRuntime/ObjectStorage story (ADR-011) |
| Approval & feedback | `transitions` (actor=user email, reason=feedback text; gate card in `payload` on `awaiting_approval` rows). Decisions arrive as `work_queue` continue items — the platform never mutates run state. Gate approval checks the user's `memberships.role` against the policy gate's role requirement. |
| Fargate integration | Deferred — `steps.fargate_task_arn` reused as the generic runtime handle (container id under DockerRuntime; task ARN once FargateRuntime lands) |
| Budget cap | `runs.cost_usd`, `steps.cost_usd` |
| Async run dispatch | `work_queue` — Platform API writes, Engine claims via SKIP LOCKED (ADR-003) |
