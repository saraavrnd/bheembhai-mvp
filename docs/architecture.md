# Architecture — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10

## Overview

BheemBhai is a governed, containerized pipeline platform that orchestrates AI-powered
product-development skills. The architecture is a **two-service system** — a Platform API that
serves the browser and manages users/projects/configuration, and an Engine Service that runs
the workflow state machine and manages the lifecycle of agent containers on AWS Fargate.

Both services are Python 3 + FastAPI, share a PostgreSQL (RDS) database, and communicate via
HTTP. The Engine Service IS the background worker — it runs the state machine loop as an
asyncio-based long-lived process, launching Fargate tasks, polling for completion, and
reconciling results. No separate queue infrastructure (SQS, Redis, Step Functions) is needed.

## Diagrams

### Component / container diagram

```
┌──────────────────────────────────────────────────────────────────────┐
│                          AWS Cloud                                   │
│                                                                      │
│  ┌──────────┐     ┌─────────────────────────────────────────────┐   │
│  │  Edge    │     │                 VPC                          │   │
│  │  Proxy   │     │                                              │   │
│  │ (ALB +   │     │  ┌──────────┐   ┌──────────┐                │   │
│  │  IdP)    │     │  │ Platform │   │  Engine  │                │   │
│  └────┬─────┘     │  │   API    │   │ Service  │                │   │
│       │           │  │ (FastAPI)│   │ (FastAPI)│                │   │
│       ▼           │  │  :8000   │   │  :8001   │                │   │
│  ┌──────────┐     │  │          │   │          │                │   │
│  │  Auth    │     │  │  Auth    │   │          │                │   │
│  │ Provider │     │  │ Provider │   │          │                │   │
│  │ (plug)   │     │  │ (JWT val)│   │          │                │   │
│  └────┬─────┘     │  └────┬─────┘   └────┬─────┘                │   │
│       │           │       │              │                       │   │
│       │           │       │    HTTP      │                       │   │
│       │           │       └──────────────┘                       │   │
│       │           │              │         │                      │   │
│  ┌────┴────┐      │              ▼         ▼                      │   │
│  │ Browser │      │  ┌──────────────────────────┐                │   │
│  │ (HTML + │      │  │      RDS PostgreSQL      │                │   │
│  │ Alpine) │      │  │  (users, projects, runs, │                │   │
│  └─────────┘      │  │   steps, transitions)    │                │   │
│                   │  └──────────────────────────┘                │   │
│                   │                                              │   │
│                   │  ┌──────────────┐  ┌──────────────┐         │   │
│                   │  │   Object     │  │   Secure     │         │   │
│                   │  │   Storage    │  │   Storage    │         │   │
│                   │  │  (pluggable) │  │  (pluggable) │         │   │
│                   │  │  (pluggable) │  │              │         │   │
│                   │  └──────────────┘  └──────────────┘         │   │
│                   │                                              │   │
│                   │  ┌──────────────────────────────────────┐    │   │
│                   │  │         AWS Fargate                  │    │   │
│                   │  │  ┌─────────┐  ┌─────────┐           │    │   │
│                   │  │  │  Agent  │  │  Agent  │  ...      │    │   │
│                   │  │  │  Task   │  │  Task   │           │    │   │
│                   │  │  │ (step)  │  │ (step)  │           │    │   │
│                   │  │  └─────────┘  └─────────┘           │    │   │
│                   │  └──────────────────────────────────────┘    │   │
│                   └──────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

### Sequence diagram — Happy-path run

```
Browser    Edge Proxy   Platform API    Postgres        Engine Service    Fargate    Obj.Storage
  │         │             │                │                │              │          │
  │ POST /runs            │                │                │              │          │
  │────────►│────────────►│                │                │              │          │
  │         │  (JWT ok)   │                │                │              │          │
  │         │             │ validate input │                │              │          │
  │         │             │ create run +   │                │              │          │
  │         │             │ first step ────►                │              │          │
  │         │             │ INSERT work_queue (action=start)│              │          │
  │         │             │───────────────►│                │              │          │
  │  201 + run_id         │                │                │              │          │
  │◄────────│◄────────────│                │                │              │          │
  │         │             │                │                │              │          │
  │         │             │                │  SELECT SKIP LOCKED          │          │
  │         │             │                │◄───────────────│              │          │
  │         │             │                │  UPDATE state='claimed'      │          │
  │         │             │                │────────────────►              │          │
  │         │             │                │                │              │          │
  │         │             │                │                │ RunTask      │          │
  │         │             │                │                │─────────────►│          │
  │         │             │                │                │ heartbeat    │          │
  │         │             │                │                │ every 30s ──►│          │
  │         │             │                │                │              │          │
  │  (poll: step=running) │                │                │              │          │
  │◄────────│◄────────────│                │                │              │          │
  │         │             │                │                │              │          │
  │         │             │                │  (agent clones, runs skill,   │       │
  │         │             │                │   commits, pushes)           │       │
  │         │             │                │                │──result──►│  │       │
  │         │             │                │                │              │          │
  │         │             │                │                │◄─task done──│          │
  │         │             │                │                │              │          │
  │         │             │                │                │ read result─┼─────────►│
  │         │             │                │                │ reconcile → classify  │
  │         │             │                │                │ persist step──────────►│
  │         │             │                │                │              │          │
  │         │             │                │                │ evaluate policy gate   │
  │         │             │                │                │ (no gate → route on)   │
  │         │             │                │                │              │          │
  │  ... (steps 2–N loop) │                │                │              │          │
  │         │             │                │                │              │          │
  │         │             │  POST /webhooks│                │              │          │
  │         │             │  run_completed │                │              │          │
  │         │             │◄────────────────────────────────│              │          │
  │         │             │                │  UPDATE work_queue state='done'          │
  │         │             │                │◄───────────────│              │          │
  │         │             │                │                │              │          │
  │  (poll: run=completed)│                │                │              │          │
  │◄────────│◄────────────│                │                │              │          │
```

### Sequence diagram — Gated run (approval required)

```
Browser    Edge Proxy   Platform API    Postgres        Engine Service      User (reviewer)
  │         │             │                │                │                    │
  │         │             │  (step N completes)             │                    │
  │         │             │                │                │                    │
  │         │             │  POST /webhooks│                │                    │
  │         │             │  approval_required (step, files)                    │
  │         │             │◄────────────────────────────────│                    │
  │         │             │                │                │                    │
  │  (poll: gate=awaiting_approval)        │                │                    │
  │◄────────│◄────────────│                │                │                    │
  │         │             │                │                │                    │
  │  (reviewer opens UI, sees gate card)   │                │                    │
  │         │             │                │                │                    │
  │  POST /runs/{id}/decision {action: approve}                                 │
  │────────►│────────────►│                │                │                    │
  │         │             │ record decision│                │                    │
  │         │             │ INSERT work_queue (action=continue)                 │
  │         │             │───────────────►│                │                    │
  │  200 OK │             │                │                │                    │
  │◄────────│◄────────────│                │                │                    │
  │         │             │                │                │                    │
  │         │             │                │  SELECT SKIP LOCKED                │
  │         │             │                │◄───────────────│                    │
  │         │             │                │  UPDATE state='claimed'            │
  │         │             │                │────────────────►                    │
  │         │             │                │                │                    │
  │         │             │                │                │ evaluate decision  │
  │         │             │                │                │ route → next step  │
  │         │             │                │                │ (or DONE)          │
  │         │             │                │                │                    │
  │  (poll: step=running on next step)     │                │                    │
  │◄────────│◄────────────│                │                │                    │
```

### State diagram — Run & step lifecycle

```
                    ┌─────────────┐
                    │   PENDING   │
                    └──────┬──────┘
                           │ start()
                           ▼
                    ┌─────────────┐
              ┌─────│   RUNNING   │◄──────────────────────┐
              │     └──────┬──────┘                       │
              │            │ step completes                │
              │            │ (gate required)               │
              │            ▼                              │
              │     ┌─────────────────┐                   │
              │     │ AWAITING_APPROVAL│                  │
              │     └────────┬────────┘                   │
              │              │ approve + route → next step│
              │              │ (not DONE)                 │
              │              └────────────────────────────┘
              │
              │            │ approve + route → DONE
              │            ▼
              │     ┌─────────────┐
              │     │  COMPLETED  │
              │     └─────────────┘
              │
              │            │ step fails (transient)
              │            ▼
              │     ┌─────────────┐
              │     │  RETRYING   │────────────────────────┐
              │     └──────┬──────┘                        │
              │            │ retry exhausted                │
              │            ▼                               │
              │     ┌─────────────┐                        │
              └─────│   FAILED    │                        │
                    └─────────────┘                        │
                                                           │
              Step-level state machine (inside RUNNING):   │
              ┌──────────┐                                 │
              │ PENDING  │                                 │
              └────┬─────┘                                 │
                   │ engine.launch_step()                  │
                   ▼                                       │
              ┌──────────┐     transient failure           │
              │ RUNNING  │─────────────────────────────────┘
              └────┬─────┘     attempt_no < MAX_VISITS
                   │
                   │ container exits
                   ▼
              ┌──────────────┐
              │ AWAITING_RESULT│──failed_incomplete (no result file)──► RETRY
              └──────┬───────┘
                     │ result payload + exit status reconciled
                     ▼
              ┌──────────────┐
              │  CLASSIFIED  │──completed / BLOCK / changes_requested /
              └──────────────┘  escalation_required → workflow routes
```

### Deployment / topology diagram

```
                          Internet
                             │
                             ▼
                     ┌────────────────┐
                     │   Route 53     │
                     │  (bheembhai...) │
                     └───────┬────────┘
                             │
                             ▼
                     ┌────────────────┐
                     │  Edge Proxy    │
                     │  (ALB + IdP)   │
                     │  JWT auth      │
                     └───────┬────────┘
                             │
                ┌────────────┼────────────┐
                │            │            │
                ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ EC2 AZ-a │ │ EC2 AZ-b │ │  (ALB    │
        │ Platform │ │ Platform │ │  internal│
        │ API      │ │ API      │ │  for     │
        │ Engine   │ │ Engine   │ │  engine) │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             │            │            │
             └────────────┼────────────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
              ▼           ▼           ▼
        ┌─────────┐ ┌─────────┐ ┌──────────┐
        │  RDS    │ │ Object  │ │ Secure   │
        │ Postgres│ │ Storage │ │ Storage  │
        │ (multi- │ │(plugg.) │ │(plugg.) │
        │  AZ)    │ │         │ │          │
        └─────────┘ └─────────┘ └──────────┘
                          │
                          ▼
                  ┌───────────────┐
                  │  ECS Fargate  │
                  │  (agent tasks)│
                  │  per-step     │
                  │  containers   │
                  └───────────────┘
```

## Components

| Component | Responsibility | Talks to | Tech |
|-----------|----------------|----------|------|
| **Platform API** | Auth (JWT validation), user/project CRUD, workflow/policy CRUD, execution history reads, approval actions, writes work items to `work_queue` (never calls Engine directly), serves HTML UI | Browser (HTTP), Postgres, Secure Storage | FastAPI + Jinja2 + SQLAlchemy |
| **Engine Service** | Workflow state machine, pulls work from `work_queue` (SKIP LOCKED), Fargate task lifecycle (launch → poll → reconcile → route), heartbeat, policy gate evaluation, failure classification, crash recovery on startup | Platform API (HTTP webhooks), Fargate (boto3), Object Storage (read results), Postgres (work queue + state) | FastAPI + boto3 + SQLAlchemy |
| **Agent Container** | One-skill execution: clone repo, run Claude Code, commit + push, publish result | Fargate (runtime), GitHub (clone/push), Jira MCP, Object Storage (write result) | Docker (node:20 + Claude Code + bash) |
| **PostgreSQL (RDS)** | All persistent state: users, projects, integrations, workflows, policies, runs, steps, transitions | Platform API, Engine Service | RDS PostgreSQL 16 |
| **Object Storage** | Pluggable artifact storage (ADR-011). Stores `bb_step_result.json`, agent logs, diagnostics. Exposes put/get/list/presigned_url operations behind a Protocol. First implementation: S3. Protocol designed for Azure Blob, MinIO, local FS. | Agent Container (write), Engine Service (read), Browser (pre-signed URLs via Platform API) | Python Protocol + boto3 (S3), azure-storage-blob, minio |
| **S3** | First ObjectStorage backend. Execution artifacts with lifecycle policy (expire after 90 days). | ObjectStorage provider (write/read), Browser (signed URLs via Platform API) | S3 Standard |
| **Secure Storage** | Pluggable credential storage (ADR-012). Stores per-integration GitHub/Jira tokens. Exposes get/put/delete behind a Protocol. First implementation: AWS Secrets Manager. Protocol designed for Azure Key Vault, HashiCorp Vault, encrypted env. | Platform API (put at integration setup), Engine Service (get at step launch) | Python Protocol + boto3 (Secrets Manager), azure-keyvault-secrets, hvac |
| **Auth Provider** | Pluggable identity verification (ADR-010). Validates JWT tokens, normalizes claims to `Identity` (external_id, email, display_name, provider). First implementation: Cognito. Protocol designed for Azure AD, Okta, etc. | Browser (via edge proxy), Platform API (middleware) | Python Protocol + `pyjwt[crypto]` + `httpx` (JWKS fetch) |
| **Edge Proxy** | TLS termination, OAuth login flow (hosted UI, redirect, callback), JWT forwarding to backend. Cognito User Pool is the first configured IdP. | Browser, Auth Provider (Cognito, Azure AD, etc.), Platform API | AWS ALB (or Azure App Gateway, nginx, etc.) |
| **ECR** | Agent Docker image storage | Fargate (pull on task launch), CI/CD (push on build) | ECR |

## Data flow

### Run submission
1. Browser POSTs `{workflow_id, policy_id, story_id, source_branch}` to Platform API
2. Platform API validates: workflow exists + belongs to project, policy exists + is tied to that workflow, source branch is valid
3. Platform API creates run row (state=pending) + first step row (state=pending)
4. Platform API INSERTs a work item into `work_queue` (action=`start`, run_id)
5. Platform API returns 201 + run_id to browser — no HTTP call to Engine
6. An Engine process picks up the work via `SELECT ... FOR UPDATE SKIP LOCKED`, sets `state = 'claimed'`, spawns `_run_loop(run_id)` as an asyncio task
7. Browser polls `/api/poll?since=<cursor>` for event stream updates

### Step execution
1. Engine loads step context: allowed result statuses, gate flag, result status meanings, upstream hand-off (if any)
2. Engine calls `FargateRuntime.launch()` → boto3 `run_task()` with env vars (RUN_ID, STEP_ID, SKILL, BB_MODEL, git coordinates, STORY_ID, MCP credentials from Secure Storage)
3. Agent container starts: writes diagnostics, clones branch (creates on step 1), runs Claude Code with `--model <tier> --dangerously-skip-permissions --mcp-config`, commits, pushes
4. Agent writes `bb_step_result.json` to Object Storage via the storage plugin (S3, Azure Blob, etc.)
5. Fargate task exits
6. Engine polls `describe_tasks()` until STOPPED, reads exit code
7. Engine reads `bb_step_result.json` from Object Storage via the configured provider
8. Reconciler joins result payload + exit status → classifies outcome (completed/BLOCK/failed_*)
9. Engine persists step outcome to Postgres (steps + transitions rows)
10. Engine evaluates policy gate for this step+status → if gate required, pause run (state=awaiting_approval), webhook to Platform API with `approval_required` event
11. If no gate, consult workflow `on:` map → route to next step or DONE

### Approval flow
1. Browser polls, sees `approval_required` event → UI shows gate card with review files + summary
2. Reviewer clicks Approve or Request Changes (optionally with comment). The reviewer's
   project membership role (from `memberships`) is checked against the policy gate's role requirement.
3. Browser POSTs `/api/runs/{id}/decision` with `{action, comment}`
4. Platform API records the decision in transitions, INSERTs a work item into `work_queue` (action=`continue`, payload=`{action, comment, actor}`)
5. An Engine process claims the work item, spawns a continuation task that evaluates the decision:
   - **Approve + route→next_step**: launch the next step in the workflow per the `on:` map.
   - **Approve + route→DONE**: complete the run.
   - **Request changes**: send the run back to the **immediately previous step** (ADR-007).
     The previous step gets a new attempt with the reviewer's feedback injected via the
     `reviewer_feedback` field in step context. The review record and older attempt history
     are preserved. This is deterministic — no workflow configuration required.
6. Engine webhooks the transition to Platform API for the event stream

### Engine crash recovery

The work queue uses a **claim + heartbeat** pattern (ADR-003). Work items are never deleted
on pickup — they transition `pending → claimed → done`. If an Engine process dies, its
heartbeat stops and the work item remains in `claimed` state. On restart, the recovery pass
converges the two sources of truth (`work_queue` and `runs`) and resumes execution.

```
Engine-1 (dies)          Fargate             Postgres               Engine-1 (restart)
    │                       │                    │                       │
    │  💥 CRASH             │                    │                       │
    │                       │  task keeps        │  work_queue:          │
    │                       │  running           │  Run-A: claimed       │
    │                       │                    │  heartbeat stale       │
    │                       │                    │  runs: Run-A=running  │
    │                       │                    │                       │
    │                       │                    │     recover_on_startup()
    │                       │                    │◄──────────────────────│
    │                       │                    │                       │
    │                       │                    │  FIND work_queue       │
    │                       │                    │  WHERE state=claimed   │
    │                       │                    │  AND heartbeat < NOW-60s│
    │                       │                    │                       │
    │                       │                    │  FIND runs WHERE       │
    │                       │                    │  state IN (running,    │
    │                       │                    │  retrying)             │
    │                       │                    │                       │
    │                       │  describe_tasks    │                       │
    │                       │◄───────────────────│                       │
    │                       │  → STOPPED         │                       │
    │                       │                    │                       │
    │                       │                    │  read result from      │
    │                       │                    │  Object Storage        │
    │                       │                    │                       │
    │                       │                    │  reconcile + route     │
    │                       │                    │  → mark work_queue.done│
    │                       │                    │  → webhook platform    │
    │                       │                    │───────────────────────►│
```

**Recovery pass** (`recover_on_startup()`, called before `worker_loop()`):

1. **Reclaim orphaned queue items:** Query `work_queue` for `state = 'claimed'` AND
   `heartbeat_at < NOW() - 60s`. Reset them to `state = 'pending'` — they re-enter the
   worker loop naturally.
2. **Reconcile in-flight runs:** Query `runs` for `state IN ('running', 'retrying')`.
   For each run's current step:
   - **Fargate task completed:** Read result from Object Storage, classify, persist step
     outcome, route to next step (or gate pause, or DONE).
   - **Fargate task still running:** Re-enqueue a work item so the live Engine picks it
     up and resumes the poll loop.
   - **No Fargate task ARN:** The Engine died before launching. Re-enqueue so the step
     is launched fresh.

## Cross-cutting concerns

| Concern | Mechanism |
|---------|-----------|
| **Auth** | Pluggable Auth Provider (ADR-010). The edge proxy (ALB, Azure App Gateway, etc.) handles the OAuth login flow with the configured IdP (Cognito first, Azure AD, Okta, etc.). The backend receives a validated JWT; the AuthProvider plugin verifies signature + expiry via JWKS and normalizes claims to `Identity` (external_id, email, display_name, provider). Platform API middleware extracts identity, looks up or creates the user row. Project-level access is governed by `memberships` (ADR-007) — a user sees only projects they are a member of. Engine Service is internal-only — authenticated via shared secret header token. |
| **Authorization** | Two-tier role model (ADR-007): `users.platform_role` (`PLATFORM_ADMIN` / `USER`) for platform-wide actions (adding project roles to the catalog per ADR-008); `memberships.role` (FK → `project_roles.key`) for project-scoped governance. Policy gates declare a required project role — the engine resolves "can this user approve at this gate?" by looking up their membership in the run's project. Project-level scoping: every query filters by `project_id` derived from the user's memberships. |
| **Logging** | Structured JSON logging (Python `logging` with `python-json-logger`). Platform API logs: request ID, user sub, route, status, latency. Engine Service logs: run_id, step_id, attempt_no, Fargate task ARN, result status. Logs shipped to CloudWatch Logs. |
| **Error handling** | Platform API: FastAPI exception handlers → consistent `{error, detail, request_id}` JSON. Engine Service: step failures classified into the fixed result status enum (transient vs deterministic); transient failures retried with backoff; deterministic failures halt the run. |
| **Config** | Environment variables (same pattern as today). Secrets never in env — fetched from Secure Storage at runtime (ADR-012). Workflow/policy YAML stored in Postgres JSONB, validated on save. |
| **Cost tracking** | Per-step model usage recorded (model_requested vs models_used from Claude Code output). Cost summed per run. Budget cap (designed, not yet built) will compare cumulative cost against per-run limit. |

## Non-functional design

| NFR | Mechanism |
|-----|-----------|
| **Authentication required** | Pluggable Auth Provider (ADR-010) + edge proxy JWT enforcement — no unauthenticated access past the login page. Cognito is the first IdP; Azure AD, Okta, etc. supported via provider plugins. |
| **Per-project credential isolation** | Each project's GitHub/Jira tokens stored in the configured SecureStorage backend (ADR-012). Engine fetches at step launch time via `credential_ref` — raw tokens never stored in Postgres or logged. Secrets Manager is the first backend; Azure Key Vault, HashiCorp Vault follow the same Protocol. |
| **Agent container isolation** | One Fargate task per step invocation — no shared filesystem between steps. Task IAM role scoped to the specific project's Object Storage prefix + Secure Storage credential |
| **Run durability** | Engine persists state to Postgres at every transition. On restart, `recover_on_startup()` reclaims orphaned `work_queue` items (stale heartbeat detection) and reconciles in-flight runs against Fargate task state — no work is lost. |
| **Artifact durability** | Pluggable ObjectStorage (ADR-011) — S3 provides 11 9s durability. Pre-signed URLs for browser access (no public bucket). Lifecycle policy: expire artifacts after 90 days. Azure Blob / MinIO backends configured per deployment. |
| **API availability** | Platform API behind ALB with health checks, multi-AZ. Engine Service on same EC2 instances, managed by systemd with auto-restart |
| **Secrets rotation** | SecureStorage backends that support rotation (Secrets Manager, Key Vault) can rotate automatically. Engine reads secrets at step launch time (not cached) so rotated tokens take effect on the next run |

## Seams for later

| Seam | Current (MVP) | Future |
|------|---------------|--------|
| **Runtime backend** | `FargateRuntime` (boto3) | `DockerRuntime` already exists for local dev; add `KubernetesRuntime` or `LambdaRuntime` behind the same protocol |
| **Database** | Single RDS instance | Read replicas for history queries; separate analytics DB (Redshift) for cost/usage dashboards |
| **Engine scaling** | One Engine process on EC2 | Multiple Engine processes behind internal ALB. `work_queue` + `SKIP LOCKED` naturally distributes work across engines. `claimed_by` column tracks ownership. |
| **Notifications** | Poll-based UI | SNS/SES for email/Slack gate notifications |
| **Multi-region** | Single region | ObjectStorage abstraction allows per-deployment choice (S3 cross-region replication, Azure RA-GRS, MinIO mirroring) |
| **Auth provider** | Cognito (first implementation); protocol designed for Azure AD, Okta, etc. | Provider plugin is a ~40-line Protocol + ~80-line concrete class. Add `azure_ad_provider.py`, flip config key `auth_provider: azure_ad`, run email-match migration. See ADR-010. |
| **Object storage** | S3 (first implementation); protocol designed for Azure Blob, MinIO, local FS. | Provider plugin is a ~50-line Protocol + ~100-line concrete class. Add `azure_blob_storage.py`, flip config key `storage_backend: azure_blob`. See ADR-011. |
| **Secure storage** | AWS Secrets Manager (first implementation); protocol designed for Azure Key Vault, HashiCorp Vault. | Provider plugin is a ~30-line Protocol + ~80-line concrete class. Add `azure_key_vault.py`, flip config key `secure_storage_backend: azure_key_vault`. See ADR-012. |

## Traceability

| EPIC BEEM-24 Feature | Covered by |
|----------------------|------------|
| AWS auth using Cognito | Pluggable Auth Provider (ADR-010) with Cognito as the first implementation. Edge proxy handles OAuth login; backend validates JWT via provider plugin. |
| Users | `users` table (platform_role); `memberships` table (project-scoped roles); `project_roles` catalog (ADR-007, ADR-008) |
| Projects + GitHub/Jira integrations | `projects` + `project_integrations` tables — multiple integrations of same type allowed per project (ADR-009); Secure Storage (ADR-012) with Secrets Manager as first backend; Platform API CRUD |
| Workflow management (create/edit) | `workflows` table (versioned YAML); Platform API CRUD; validation on save |
| Policy management (create/edit) | `policies` table (versioned YAML, tied to workflow FK); Platform API CRUD; pairing validation on save |
| Executions — view past results | `runs` + `steps` tables; Platform API history endpoints with pagination |
| Execution — detailed results | Object Storage artifacts (agent logs, diagnostics, summaries); pre-signed URLs via provider plugin (ADR-011). S3 is the first backend. |
| Approval & feedback flows | Gate cards in UI; `/runs/{id}/decision` endpoint; `request_changes` routes to previous step per ADR-007 |
| Fargate integration | `FargateRuntime` implementing existing `Runtime` protocol; boto3 `run_task`/`describe_tasks`; Engine crash recovery re-attaches to task ARNs from Postgres |
| Per-run budget cap | `cost_usd` columns on runs/steps; cap enforcement (designed, build deferred) |
| Async run dispatch | `work_queue` table (ADR-003); Platform API writes, Engine pulls via SKIP LOCKED; claim+heartbeat for crash recovery |
