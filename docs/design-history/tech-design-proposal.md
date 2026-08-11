# Technical Design Proposal — BheemBhai MVP

**Status:** Approved · **Source:** EPIC BEEM-24 + CLAUDE.md audit · **Date:** 2026-08-10

> This is a proposal. Nothing is committed until you approve. Decisions needing your input are
> listed in §7. Change anything; I'll revise and re-confirm before writing the final design.

## 1. System summary

BheemBhai is a governed, containerized pipeline platform that orchestrates AI-powered
product-development skills. Today it runs as a single-instance tool on local Docker — one user,
one workflow, one policy, no auth. EPIC BEEM-24 defines the real MVP: a **multi-tenant SaaS
platform** where named users manage projects (each wired to GitHub + Jira), configure workflows
and policies, trigger governed skill runs, review outputs at approval gates, and inspect
execution history — with the engine dispatching agent containers to **AWS Fargate** instead of a
local Docker socket.

The core pipeline engine (workflow state machine, result reconciliation, policy gates, model
enforcement) is sound and carries forward. The work is adding the **platform layer** around it:
auth, users, projects, CRUD for workflows/policies, execution history at scale, and a remote
container runtime.

## 2. MVP vs Later (scope the design)

| MVP (BEEM-24 — design for this) | Later (leave seams only) |
|---------------------------------|--------------------------|
| Cognito authentication (ALB + JWT) | SAML/SSO enterprise IdP |
| Users with roles (any / lead) | Fine-grained RBAC, teams |
| Projects with GitHub + Jira integration | GitLab, Bitbucket, Linear |
| Workflow CRUD (create, edit, delete) | Visual workflow builder |
| Policy CRUD (create, edit, toggle) | Custom policy DSL |
| Execution history with detail views | Analytics dashboard, cost reports |
| Approval gate UI (approve / request changes) | Slack/email gate notifications |
| Fargate runtime for agent containers | GCP/Azure runtimes, hybrid |
| Per-user/project run scoping | Org-level dashboards |
| Per-run budget cap | Budget forecasting |
| TDD test coverage on new code | Full regression suite |

## 3. Decisions, with options & recommendation

### 3.1 Language & framework (backend)

| Option | Pros | Cons |
|--------|------|------|
| **Python 3 + FastAPI (stay)** | Already running; team knows it; async-native; Pydantic validation; excellent Docker/Fargate SDKs; engine.py carries forward | GIL limits CPU-bound work (not our profile — orchestration is I/O-bound) |
| Node.js + Express/Fastify | Same language as agent container; large ecosystem | Rewrite engine.py; weaker typed validation; two runtimes to maintain (Python for data, Node for web) is fine but pointless here |
| Go + Chi/Fiber | Fast, single binary deploy, great concurrency | Full rewrite of 873-line engine; team context switch; overkill for an I/O orchestration workload |

**Recommendation:** **Python 3 + FastAPI.** The engine is the asset — it already works. The
new platform layer (auth middleware, project CRUD, history APIs) slots into FastAPI naturally.
No rewrite, no context switch.

### 3.2 Frontend

**Decision: Server-rendered HTML (Jinja2) + EduAdmin Bootstrap-5 theme + Alpine.js 3.**

Following the existing `docs/ui-conventions.md` from the Learn Portal project. The stack is:
- **Templating:** Jinja2 (FastAPI `Jinja2Templates`), server-rendered — no SPA.
- **Theme:** EduAdmin (purchased, ThemeForest), **semidark variant** (`main-semidark/`).
  Theme artifacts are vendored from the learn-portal reference at
  `../../ui_theme/themeforest-JVDUgCuV-eduadmin-responsive-bootstrap-admin-template-dashboard/bs5/`.
  The reference demo markup is at `main-semidark/` — every UI element (tables, forms, widgets,
  cards, auth pages) is adapted from the theme's own demo HTML, not hand-invented.
- **Styling:** Bootstrap 5.3 (vendored with theme), Bootstrap utility classes as the baseline,
  no inline styles or custom pixel values.
- **Interactivity:** Alpine.js 3.x for client behavior (form state, toggles, polling, gate
  actions). Keep it lightweight — Alpine for sprinkles, not an app.
- **Diagrams:** Mermaid.js, client-side render.
- **Base layouts:** `base.html` (authenticated: fixed header + collapsible sidebar) and
  `auth_base.html` (anonymous: centered card, no chrome).
- **Vendor assets** live at `app/static/vendor/eduadmin/` — theme files kept unmodified so
  updates stay drop-in. Project overrides in `app/static/css/` only.
- **Design-system discipline:** use Bootstrap's design tokens (spacing scale, semantic color
  classes, typography classes), Bootstrap components (buttons, forms, cards, modals, alerts,
  nav, tables), and Bootstrap grid/breakpoints. Don't hand-roll what Bootstrap already provides.

This replaces the current 550-line vanilla HTML dashboard. The EduAdmin theme gives us a
production admin UI without a design system build-out.

### 3.3 Architecture style

**Decision: Two-service architecture — Platform API + Engine Service.**

| Component | Role | Runtime |
|-----------|------|---------|
| **Platform API** (`backend/platform/`) | Auth (JWT validation), user management, project CRUD, workflow/policy CRUD, execution history reads, approval actions. This is what the browser talks to. | EC2 (FastAPI) |
| **Engine Service** (`backend/engine/`) | Workflow state machine, Fargate task lifecycle (launch → poll → reconcile → route), policy gate evaluation, event bus. This is an internal service — no direct browser access. | EC2 (FastAPI, separate process or port) |

**Communication pattern:**
- Platform API → Engine: HTTP POST to start a run, HTTP POST to continue after human approval.
  Synchronous request, asynchronous work (engine returns 202 Accepted immediately).
- Engine → Platform API: HTTP POST webhook when a run hits a gate (publishes `approval_required`
  event that the platform stores and surfaces to the UI).
- Engine internally: runs the state machine loop as asyncio tasks. Each step: launch Fargate
  task via boto3 → poll `describe_tasks` for completion → read result from S3 → reconcile →
  determine next step from workflow `on:` map → persist state to Postgres → repeat or pause.

**Why two services, not a monolith:**
- The engine's workload is fundamentally different from the platform API: long-lived async
  state-machine loops vs short request-response CRUD.
- Independent scaling: platform API scales with users/browsers; engine scales with concurrent
  runs.
- Independent failure domains: if the engine restarts, it recovers in-flight runs from Postgres
  state; the platform API stays up for users.
- Clean API contract between them makes the engine testable in isolation (FakeRuntime carries
  forward).

**Why not SQS/Step Functions for the engine loop:**
- The engine's existing state machine logic (873 lines, tested) is the core IP — replacing it
  with Step Functions discards tested code and couples the workflow definition to AWS infra.
- SQS between services adds a queue to manage, DLQ to monitor, and async handshake complexity
  for the approval loop (platform → engine "continue" needs a response path).
- Direct HTTP between the two services is simpler, synchronous where it needs to be (kick off a
  run, continue after approval), and asynchronous where it matters (run execution). The engine
  persists state at every transition, so restarts are safe.

### 3.4 Data stores

| Option | Pros | Cons |
|--------|------|------|
| **PostgreSQL (RDS)** | Multi-tenancy support; concurrent reads/writes; JSONB for flexible result payloads; migrations; already named in README as the target | Operational overhead vs SQLite (but required for multi-user) |
| SQLite (stay) | Zero ops; simple | Single-writer; no concurrent access; no user isolation; won't survive multi-tenancy |
| DynamoDB | Serverless; scales automatically | Query patterns for history/audit are relational; would reinvent joins in application code |

**Decision: PostgreSQL (RDS).** Both services share one Postgres instance with separate
schemas or table prefixes. SQLAlchemy 2.0 + Alembic for migrations. The current SQLite schema
(runs, steps, transitions) migrates directly. New tables: users, projects,
project_integrations, workflows, policies, workflow_versions, policy_versions. JSONB for
result payloads and workflow/policy YAML definitions. SQLite stays only for `test_engine.py`
(in-memory `FakeRuntime` tests).

### 3.5 Key libraries / external services

| Concern | Choice | Why |
|---------|--------|-----|
| Auth | AWS Cognito + ALB JWT validation | Already designed in README; ALB handles OAuth flow; backend validates JWT claims in middleware |
| ORM | SQLAlchemy 2.0 + Alembic | Async support; mature migration tooling; FastAPI ecosystem standard |
| Templating | Jinja2 (FastAPI `Jinja2Templates`) | Server-rendered HTML per UI conventions |
| Interactivity | Alpine.js 3.x | Lightweight client behavior; vendored at `app/static/vendor/alpine/` |
| API docs | FastAPI auto-generated OpenAPI | Already built in |
| GitHub integration | GitHub MCP (existing) + PyGithub for project setup | MCP is already wired in agent; PyGithub for verifying repo access during project setup |
| Jira integration | Atlassian MCP (existing) | Already wired; needs project-level credential storage (encrypted in Secrets Manager) |
| Container runtime | AWS Fargate via boto3 | `FargateRuntime` implements the existing `Runtime` protocol (launch/status/logs/cleanup) |
| Secrets | AWS Secrets Manager | Per-project credentials (GH token, Jira token) stored as encrypted secrets, referenced by ARN |
| Artifact storage | S3 | Execution artifacts (result JSON, agent logs, diagnostics) written to S3 by the agent container via SDK; survive container teardown; signed URLs for UI file viewer |
| Service-to-service auth | Shared secret (header token) | Engine and Platform API share a secret; ALB ensures only internal traffic reaches the engine |

### 3.6 Deployment target

**Decision: EC2 + ALB + RDS + Fargate (hybrid).**

| Component | Where | Why |
|-----------|-------|-----|
| Platform API | EC2 (single instance, auto-scaling group for HA) | Low traffic; simple; can move to ECS later |
| Engine Service | EC2 (same or separate instance) | Long-lived process managing Fargate tasks; needs reliable uptime |
| Agent containers | AWS Fargate (one task per step invocation) | Isolated, scalable, pay-per-use; matches the one-container-per-step model |
| Database | RDS Postgres 16.x | Managed; backups; multi-AZ for HA |
| Secrets | AWS Secrets Manager | Per-project credentials encrypted at rest |
| Artifacts | S3 | Durable; signed URLs for UI access |
| Auth | Cognito User Pool + ALB | ALB validates JWT at the edge before traffic reaches the backend |

### 3.7 Testing approach

| Option | Pros | Cons |
|--------|------|------|
| **TDD (pytest + Playwright)** | Catches regressions early; tests document behaviour; aligns with `test-creator` / `test-verify` skills | Discipline overhead; slower initial velocity (pays back on feature 3+) |
| Test-after (write code, then tests) | Faster initial feature velocity | Tests become optional; coverage drifts; `test-verify` has no baseline |
| No automated tests | Fastest initially | Non-starter for a platform that runs other people's code |

**Decision: TDD** across three layers:
- **Unit**: `pytest` + `pytest-asyncio` for services, engine logic, policy evaluation
- **Integration**: `pytest` + FastAPI TestClient against a test Postgres DB; `FakeRuntime` (already exists) for engine tests without real containers
- **E2E**: Playwright for critical user journeys (login → create project → run workflow → approve gate → view results)

Frontend is server-rendered HTML with Alpine.js — component testing is less relevant (no
React components). E2E tests cover the UI flows.

### 3.8 Repository shape

**Decision: Monorepo** with a clear service boundary:

```
bheembhai-mvp/
├── backend/
│   ├── platform/          # Platform API: auth, users, projects, CRUD, webhooks
│   │   ├── routes/        # FastAPI route modules
│   │   ├── models/        # SQLAlchemy models (users, projects, integrations, workflows, policies)
│   │   ├── services/      # Business logic
│   │   └── middleware/    # JWT validation, project scoping
│   ├── engine/            # Engine Service: state machine, runtime, policy, event bus
│   │   ├── runtime/       # Runtime protocol + FargateRuntime + FakeRuntime
│   │   ├── policy/        # Policy loading, evaluation, gate logic
│   │   └── workflow/      # Workflow loading, validation, routing
│   └── shared/            # Shared: DB session, base models, config types
├── app/
│   ├── templates/         # Jinja2 templates (base layouts, pages, partials)
│   ├── static/
│   │   ├── vendor/        # EduAdmin theme, Bootstrap, Alpine.js, Mermaid.js
│   │   └── css/           # Project-specific CSS overrides
│   └── main.py            # FastAPI app factory + mount points
├── agent/                 # unchanged — Docker image, skills, run_skill.sh
├── config/                # seed workflows, policies, profiles
├── docs/                  # architecture, ADRs, data-model, api-contracts
├── tests/                 # integration + e2e
├── docker-compose.yml     # local dev: platform + engine + postgres
└── .env.example
```

## 4. Proposed architecture (overview)

```
                          ┌──────────────────────────────────────────┐
                          │           AWS Cloud                      │
                          │                                          │
  User Browser ─────────► ALB (Cognito JWT validation)              │
  (HTML + Alpine)         │                                          │
                          ├─────────┬────────────────────┐           │
                          ▼         ▼                    ▼           │
                    Platform API  Engine Service    S3 (artifacts)   │
                    (EC2)         (EC2)                              │
                      │   │         │   │                            │
                      │   │  HTTP   │   │                            │
                      │   └────────┘   │                            │
                      │   (start run,  │                            │
                      │    approve)    │                            │
                      │                │                            │
                      ▼                ▼                            │
                 RDS Postgres    AWS Fargate                        │
                 (users, projects, (agent containers)               │
                  runs, steps,     │                                │
                  transitions)     │                                │
                                   │                                │
                          AWS Secrets Manager                       │
                          (per-project tokens)                      │
                          └──────────────────────────────────────────┘
```

### Sequence: Run lifecycle

```
Browser          Platform API       Engine Service      Fargate        S3
  │                   │                   │                │            │
  │ POST /runs        │                   │                │            │
  │──────────────────►│                   │                │            │
  │                   │ POST /engine/runs │                │            │
  │                   │──────────────────►│                │            │
  │                   │   202 Accepted    │                │            │
  │   {run_id}        │◄──────────────────│                │            │
  │◄──────────────────│                   │                │            │
  │                   │                   │ RunTask        │            │
  │                   │                   │───────────────►│            │
  │                   │                   │  (skill runs)  │            │
  │                   │                   │                │──result──►│
  │                   │                   │◄──task done────│            │
  │                   │                   │                │            │
  │                   │                   │ read result    │            │
  │                   │                   │───────────────────────────►│
  │                   │                   │◄───────────────────────────│
  │                   │                   │                │            │
  │                   │                   │ reconcile → classify       │
  │                   │                   │ evaluate policy gate       │
  │                   │                   │                │            │
  │                   │  POST /webhooks   │                │            │
  │                   │  approval_required│                │            │
  │                   │◄──────────────────│                │            │
  │                   │                   │                │            │
  │  (poll: gate)     │                   │                │            │
  │◄──────────────────│                   │                │            │
  │                   │                   │                │            │
  │  (user approves in UI)                │                │            │
  │ POST /runs/{id}/decision              │                │            │
  │──────────────────►│                   │                │            │
  │                   │ POST /engine/runs/{id}/continue    │            │
  │                   │──────────────────►│                │            │
  │                   │                   │ route → next step → loop   │
  │                   │                   │                │            │
  │                   │                   │  ... or DONE   │            │
  │                   │  POST /webhooks   │                │            │
  │                   │  run_completed    │                │            │
  │                   │◄──────────────────│                │            │
```

## 5. Proposed data model (sketch)

```
users
  id, cognito_sub (unique), email, display_name, role (any|lead), created_at

projects
  id, name, owner_id (FK users), created_at

project_integrations
  id, project_id (FK projects), type (github|jira),
  secret_arn (Secrets Manager ref), config (JSONB: repo_url, jira_url, etc.), verified_at

workflows (per-project, versioned)
  id, project_id (FK projects), version, name, yaml_content, is_active, created_at

policies (per-project, versioned, tied to a specific workflow)
  id, project_id (FK projects), workflow_id (FK workflows, NOT NULL),
  version, name, yaml_content, is_active, created_at
  -- A policy must be tied to one workflow so validation (pairing gates to
  -- routing targets) is correct — a policy gating on a status the workflow
  -- can't route from that step is rejected at save time.

runs (existing, extended)
  id, project_id (FK projects), workflow_id, policy_id,
  story_id, source_branch, run_branch, state, cost_usd, created_at

steps (existing, extended)
  id, run_id (FK runs), step_id, skill, exec_state, result_status,
  model_requested, models_used, cost_usd, attempt_no,
  fargate_task_arn, artifact_s3_key, started_at, ended_at

transitions (existing, unchanged)
  id, run_id, step_id, attempt_no, from_state, to_state,
  result_status, actor, reason, ts
```

## 6. Versions (pinned)

| Component | Version | Notes |
|-----------|---------|-------|
| Python | 3.12 | Current stable |
| FastAPI | 0.115.x | Pydantic v2 built in |
| SQLAlchemy | 2.0.x | Async-native ORM |
| Alembic | 1.14.x | Migration management |
| Jinja2 | 3.1.x | Built into FastAPI |
| Bootstrap | 5.3.x | Vendored with EduAdmin theme |
| Alpine.js | 3.14.x | Vendored at `app/static/vendor/alpine/` |
| Mermaid.js | 11.x | Client-side render, vendored |
| PostgreSQL | 16.x (RDS) | Latest RDS-supported major |
| boto3 | 1.36.x | AWS SDK (Fargate, S3, Secrets Manager) |
| PyGithub | 2.x | GitHub REST API for project integration setup |
| Playwright | 1.50.x | E2E testing |
| Docker (agent image) | node:20-slim + Claude Code latest | Unchanged from current; pushed to ECR |

## 7. Decisions (resolved)

- [x] **D1 — Frontend.** Server-rendered HTML (Jinja2) + EduAdmin Bootstrap-5 theme (semidark) + Alpine.js 3. Follow existing `docs/ui-conventions.md`. Copy theme artifacts from learn-portal reference.
- [x] **D2 — Architecture.** Two services: Platform API + Engine Service, communicating via HTTP. Engine owns the state machine loop; no SQS/Step Functions.
- [x] **D3 — Deployment.** EC2 + ALB + RDS + Fargate hybrid.
- [x] **D4 — Background jobs.** The Engine Service itself is the background worker — it's a long-lived service that manages Fargate task lifecycles directly via boto3 (launch → poll → reconcile → route). No separate queue infrastructure needed. The Platform API calls the Engine synchronously (POST to start/continue); the Engine does the async work and webhooks back when a human gate is hit.
- [x] **D5 — Database.** PostgreSQL RDS 16.x.
- [x] **D6 — Artifact storage.** S3 (agent writes results/logs directly; signed URLs for UI access).
- [x] **D7 — UI conventions.** Follow existing `docs/ui-conventions.md`. EduAdmin theme artifacts copied from learn-portal reference at `../../ui_theme/themeforest-JVDUgCuV-eduadmin-responsive-bootstrap-admin-template-dashboard/bs5/main-semidark/`.

## 8. Assumptions made

- **Single AWS account.** Cognito, ALB, EC2, ECS/Fargate, RDS, S3, Secrets Manager all in one account.
- **Internal tool, not SaaS product.** Cognito via ALB for auth; no public sign-up, billing, or hard tenant isolation beyond project scoping.
- **GitHub + Jira only for MVP.** GitLab/Bitbucket/Linear are later.
- **One agent image.** The current `bheembhai/agent:latest` image carries forward, pushed to ECR for Fargate.
- **Per-project credentials in AWS Secrets Manager.** GitHub token and Jira token are encrypted secrets, referenced by ARN in the DB — never stored in plaintext.
- **Engine recovers from Postgres state.** On restart, the engine queries for in-flight runs (state != completed/failed) and resumes the state-machine loop. Fargate task ARNs stored in the step row let it re-attach.
- **Cognito user pool already exists or will be created.** Design assumes the pool + ALB integration; CloudFormation/CDK is infra work separate from the application.
- **Workflow/policy YAML format stays.** CRUD means storing, versioning, and editing YAML — not changing the format the engine parses.
- **UI theme artifacts are already available** at the learn-portal reference path and will be vendored into this repo under `app/static/vendor/eduadmin/`.

---
*After approval, the `tech-design` skill writes the committed design (architecture.md, ADRs,
data-model.md, api-contracts/, tech-stack.md, testing-strategy.md, ui-conventions.md), which
`project-scaffold` then turns into a runnable repository.*
