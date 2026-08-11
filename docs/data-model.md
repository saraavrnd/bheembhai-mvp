# Data Model — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10 · **Database:** PostgreSQL 16 (RDS)

## Entity-Relationship Diagram

```
users 1───* projects            (owner_id)
users 1───* memberships         (user's project roles)
projects 1───* memberships      (project members)
project_roles 1───* memberships (role key)
projects 1───* project_integrations
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
| `type` | TEXT | NOT NULL, CHECK (type IN ('github', 'jira')) | |
| `label` | TEXT | NOT NULL | Human-readable label to distinguish multiple integrations of the same type (e.g., "Backend Board", "Frontend Repo") |
| `credential_ref` | TEXT | NOT NULL | Opaque, provider-specific reference to stored credentials (Secrets Manager ARN, Key Vault URL, Vault path). The raw token is never in the DB. ADR-012. |
| `config` | JSONB | NOT NULL, DEFAULT '{}' | Integration-specific: `{repo_url, repo_owner, repo_name}` for GitHub; `{jira_url, project_key}` for Jira |
| `verified_at` | TIMESTAMPTZ | | NULL until the project owner verifies the integration works |
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
| `source_branch` | TEXT | NOT NULL | Git branch the run branch was cut from |
| `run_branch` | TEXT | NOT NULL | `feat/<story>/<DDMMYYYYHHmm>` — the run owns this branch |
| `state` | TEXT | NOT NULL, DEFAULT 'pending' | Run-level state (pending/running/awaiting_approval/retrying/completed/failed) |
| `current_step` | TEXT | | The step_id currently executing |
| `cost_usd` | NUMERIC(10,4) | NOT NULL, DEFAULT 0 | Cumulative cost across all steps |
| `created_at` | TIMESTAMPTZ | NOT NULL, DEFAULT NOW() | |

### steps

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | UUID | PK, DEFAULT gen_random_uuid() | |
| `run_id` | UUID | FK → runs.id ON DELETE CASCADE, NOT NULL | |
| `step_id` | TEXT | NOT NULL | Matches the workflow step `id` (e.g. "story-design") |
| `skill` | TEXT | NOT NULL | The skill invoked (e.g. "story-design") |
| `exec_state` | TEXT | NOT NULL, DEFAULT 'pending' | Step-level state (pending/running/awaiting_result/retrying/completed/failed) |
| `result_status` | TEXT | | The classified outcome from the result status enum |
| `model_requested` | TEXT | | The model tier from the workflow (e.g. "claude-opus-4-8") |
| `models_used` | TEXT | | Comma-separated models actually used (from Claude Code output) |
| `cost_usd` | NUMERIC(10,4) | NOT NULL, DEFAULT 0 | |
| `attempt_no` | INTEGER | NOT NULL, DEFAULT 1 | |
| `fargate_task_arn` | TEXT | | AWS Fargate task ARN — for re-attachment on Engine restart |
| `artifact_storage_key` | TEXT | | Opaque storage key prefix for this step's artifacts (e.g. `runs/<run_id>/<step_id>/<attempt_no>/`). The backend prefix (S3 bucket, Azure container, local path) is provider config per ADR-011. |
| `started_at` | TIMESTAMPTZ | | |
| `ended_at` | TIMESTAMPTZ | | |

### transitions

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | BIGSERIAL | PK | |
| `run_id` | UUID | FK → runs.id, NOT NULL | |
| `step_id` | TEXT | NOT NULL | |
| `attempt_no` | INTEGER | NOT NULL | |
| `from_state` | TEXT | NOT NULL | |
| `to_state` | TEXT | NOT NULL | |
| `result_status` | TEXT | | The result status that triggered this transition |
| `actor` | TEXT | NOT NULL, DEFAULT 'system' | 'system' or user email (for approval decisions) |
| `reason` | TEXT | | Human-readable reason for the transition |
| `ts` | REAL | NOT NULL | Unix timestamp |

This table is an append-only audit log — every state change is recorded.

### work_queue

ADR-003: Postgres-backed FIFO work queue. The Platform API INSERTs work items; Engine
processes claim them via `SELECT ... FOR UPDATE SKIP LOCKED`. Claim + heartbeat pattern
ensures crash recovery — stale claims are re-enqueued on Engine restart.

| Column | Type | Constraints | Notes |
|--------|------|-------------|-------|
| `id` | BIGSERIAL | PK | |
| `run_id` | UUID | FK → runs.id, NOT NULL | The run to start or continue |
| `action` | TEXT | NOT NULL, CHECK (action IN ('start', 'continue')) | `start` = new run; `continue` = resume after approval gate |
| `payload` | JSONB | NOT NULL, DEFAULT '{}' | For `start`: `{story_id, source_branch}`. For `continue`: `{action, comment, actor}`. |
| `state` | TEXT | NOT NULL, DEFAULT 'pending', CHECK (state IN ('pending', 'claimed', 'done')) | `pending` = unclaimed; `claimed` = being processed; `done` = run reached terminal state |
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

-- Query: "in-flight runs on Engine restart"
CREATE INDEX idx_runs_state ON runs (state) WHERE state IN ('running', 'retrying', 'awaiting_approval');

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
  "repo_url": "https://github.com/owner/repo",
  "repo_owner": "owner",
  "repo_name": "repo",
  "default_branch": "main"
}
```

### project_integrations.config (Jira)
```json
{
  "jira_url": "https://company.atlassian.net",
  "project_key": "BEEM"
}
```

## Migrations from existing SQLite

The existing schema maps 1:1 to Postgres with minimal changes:

| SQLite | Postgres |
|--------|----------|
| `TEXT PRIMARY KEY` for run/step IDs | `UUID PRIMARY KEY DEFAULT gen_random_uuid()` |
| `REAL` for cost | `NUMERIC(10,4)` for exact money |
| `REAL` for timestamps | `TIMESTAMPTZ` |
| No FKs | Foreign keys with CASCADE where appropriate |
| No JSONB | `config` and `result` fields as JSONB |

The `test_engine.py` FakeRuntime tests continue using SQLite in-memory — the SQLAlchemy
abstraction means the same model code works against both backends.

## Traceability

| BEEM-24 Feature | Tables |
|-----------------|--------|
| AWS Cognito auth | `users` (external_id, auth_provider). AuthProvider protocol abstracts the provider; Cognito is the first implementation (ADR-010). |
| Users | `users`, `memberships` (project-scoped roles) |
| Projects | `projects`, `project_integrations`, `memberships` |
| Project roles | `project_roles` (catalog), `memberships` (assignment) — ADR-007, ADR-008 |
| GitHub/Jira integrations | `project_integrations` (credential_ref → SecureStorage). Pluggable protocol (ADR-012) — Secrets Manager is the first backend. |
| Workflow management | `workflows` (versioned YAML, is_active) |
| Policy management | `policies` (versioned YAML, is_active, FK → workflows) |
| Executions — history | `runs` (per project, paginated), `steps`, `transitions` |
| Executions — detail | Artifact storage keys in `steps.artifact_storage_key`. Pluggable ObjectStorage provider (ADR-011) — S3 is the first backend. |
| Approval & feedback | `transitions` (actor=user email, reason=feedback text). Gate approval checks the user's `memberships.role` against the policy gate's role requirement. |
| Fargate integration | `steps.fargate_task_arn` |
| Budget cap | `runs.cost_usd`, `steps.cost_usd` |
| Async run dispatch | `work_queue` — Platform API writes, Engine claims via SKIP LOCKED (ADR-003) |
