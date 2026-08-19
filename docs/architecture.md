# Architecture — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10

## Overview

BheemBhai is a governed, containerized pipeline platform that orchestrates AI-powered
product-development skills. The architecture is a **two-service system** — a Platform API that
serves the browser and manages users/projects/configuration, and an Engine Service that runs
the workflow state machine and manages the lifecycle of agent containers on AWS Fargate.

Both services are Python 3 + FastAPI, share a PostgreSQL (RDS) database, and communicate via
HTTP. The Engine Service IS the background worker — it runs the state machine loop as an
asyncio-based long-lived process, launching agent containers behind a pluggable **Runtime
protocol** (ADR-013 §4: `DockerRuntime` for local dev today; `FargateRuntime` deferred),
polling for completion, and reconciling results. No separate queue infrastructure (SQS,
Redis, Step Functions) is needed. Engine → platform events are fire-and-forget webhook POSTs
(`POST /webhooks/engine`, shared `X-BB-Secret`, bounded queue + one retry — losing one only
costs a poll interval, since the UI polls the DB).

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
│       ▼           │  │  :9000   │   │  :9001   │                │   │
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
Browser    Edge Proxy   Platform API    Postgres        Engine Service    Runtime    GitHub API
  │         │             │                │                │              │          │
  │ POST /runs            │                │                │              │          │
  │────────►│────────────►│                │                │              │          │
  │         │  (JWT ok)   │                │                │              │          │
  │         │             │ validate input │                │              │          │
  │         │             │ create run ────►                │              │          │
  │         │             │ (pending, run_branch=NULL)      │              │          │
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
  │         │             │                │                │ _init_run: create branch   │
  │         │             │                │                │ (idempotent)│─────────────►│
  │         │             │                │                │ persist run_branch + all    │
  │         │             │                │                │ step rows ─►│              │
  │         │             │                │                │              │          │
  │         │             │                │                │ launch container           │
  │         │             │                │                │─────────────►│          │
  │         │             │                │                │ heartbeat    │          │
  │         │             │                │                │ every 30s ──►│          │
  │         │             │                │                │              │          │
  │  (poll: step=running) │                │                │              │          │
  │◄────────│◄────────────│                │                │              │          │
  │         │             │                │                │              │          │
  │         │             │                │  (agent clones the pre-existing branch,     │
  │         │             │                │   runs skill, commits, pushes)             │
  │         │             │                │                │──result──►│  │          │
  │         │             │                │                │ (mounted /out)            │
  │         │             │                │                │◄─task done──│          │
  │         │             │                │                │              │          │
  │         │             │                │                │ read result from /out     │
  │         │             │                │                │ reconcile → classify  │   │
  │         │             │                │                │ persist step──────────►│   │
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
  │         │             │ validate state=paused, then INSERT                   │
  │         │             │ work_queue (action=continue) — no state change       │
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
                           │ engine init (_init_run)
                           ▼
                    ┌─────────────┐          continue{action: approve}
                    │   RUNNING   │◄─────────────────────────────┐
                    └──────┬──────┘                              │
                           │ step completes (gate required)      │
                           ▼                                     │
                    ┌─────────────┐                              │
                    │    PAUSED   │──────────────────────────────┘
                    └──────┬──────┘
                           │ last step completes / approve → DONE
                           ▼
                    ┌─────────────┐
                    │  COMPLETED  │
                    └─────────────┘
                           ▲
              ┌────────────┼─────────────┐
              │ step fails (transient),  │ init failure (zero containers),
              │ retry exhausted /        │ non-happy verdict with no route,
              │ visit cap hit            │ escalation path exhausted
              │                          │
              │                          ▼
              │                   ┌─────────────┐
              └───────────────────│   FAILED    │
                                  └─────────────┘

              Step-level state machine (transient retries live INSIDE RUNNING —
              the run stays running while a step retries, attempt_no < MAX_VISITS):
              ┌──────────┐
              │ PENDING  │
              └────┬─────┘
                   │ engine.launch_step()          transient failure
                   ▼                               (failed_infra / failed_timeout /
              ┌──────────┐                         failed_incomplete)
              │ RUNNING  │─────────────────────────────────┘
              └────┬─────┘      attempt_no < MAX_VISITS
                   │
                   │ container exits
                   ▼
              ┌──────────────┐
              │ AWAITING_RESULT│──failed_incomplete (no result file)──► retry
              └──────┬───────┘
                     │ result payload + exit status reconciled
                     ▼
              ┌──────────────┐
              │  CLASSIFIED  │──completed / BLOCK / changes_requested /
              └──────────────┘  escalation_required → workflow routes
```

Notes on the run-level machine (ADR-013 implementation, BEEM-24):

- **Gate pause is `runs.state = "paused"`** — there is no run-level `awaiting_approval`
  state. The gated step row keeps `exec_state = "completed"` (the UI renders
  `is_awaiting_review` from that); the gate card is persisted in the `awaiting_approval`
  **transition's** JSONB payload so it survives engine restarts.
- **Retrying is not a run-level state** — transient failures retry the step
  (`attempt_no` increments) while the run stays `running`. Deterministic failures fail
  the run immediately.
- **Decisions are continue items** — `paused` is exited when the engine claims a
  `work_queue` item with `action="continue"` (`approve` routes via the `on:` map;
  `send_back` rewinds to the named target; `resume` re-attaches/relaunches after a
  crash). The platform never mutates run state directly.

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
| **Engine Service** | Workflow state machine, pulls work from `work_queue` (SKIP LOCKED), run init (branch creation via GitHub REST, model resolution), container lifecycle behind the Runtime protocol (launch → poll → reconcile → route), heartbeat, policy gate evaluation, failure classification, crash recovery on startup | Platform API (HTTP webhooks), Runtime (`DockerRuntime` today via docker-py; `FargateRuntime` deferred), GitHub REST API, Secure Storage, Postgres (work queue + state) | FastAPI + SQLAlchemy |
| **Agent Container** | One-skill execution: clone the pre-existing run branch, download the step's skill bundle (presigned S3 URL), run Claude Code, commit + push, publish result | Runtime (Docker today; Fargate later), GitHub (clone/push), Object Storage (skill bundle GET), Jira MCP | Docker (node:20 + Claude Code + bash) |
| **PostgreSQL (RDS)** | All persistent state: users, projects, integrations, workflows, policies, runs, steps, transitions | Platform API, Engine Service | RDS PostgreSQL 16 |
| **Object Storage** | Pluggable artifact storage (ADR-011). Stores skill bundles (content-addressed tar.gz, ADR-013 §3), `bb_step_result.json`, agent logs, diagnostics. Exposes put/get/list/presigned_url operations behind a Protocol. First implementation: S3. Protocol designed for Azure Blob, MinIO, local FS. | Platform API (skill bundle put), Engine Service (presign + read), Agent Container (skill bundle GET), Browser (pre-signed URLs via Platform API) | Python Protocol + boto3 (S3), azure-storage-blob, minio |
| **S3** | First ObjectStorage backend. Execution artifacts with lifecycle policy (expire after 90 days). | ObjectStorage provider (write/read), Browser (signed URLs via Platform API) | S3 Standard |
| **Secure Storage** | Pluggable credential storage (ADR-012). Stores per-integration GitHub/Jira tokens. Exposes get/put/delete behind a Protocol. First implementation: AWS Secrets Manager. Protocol designed for Azure Key Vault, HashiCorp Vault, encrypted env. | Platform API (put at integration setup), Engine Service (get at step launch) | Python Protocol + boto3 (Secrets Manager), azure-keyvault-secrets, hvac |
| **Auth Provider** | Pluggable identity verification (ADR-010). Validates JWT tokens, normalizes claims to `Identity` (external_id, email, display_name, provider). First implementation: Cognito. Protocol designed for Azure AD, Okta, etc. | Browser (via edge proxy), Platform API (middleware) | Python Protocol + `pyjwt[crypto]` + `httpx` (JWKS fetch) |
| **Edge Proxy** | TLS termination, OAuth login flow (hosted UI, redirect, callback), JWT forwarding to backend. Cognito User Pool is the first configured IdP. | Browser, Auth Provider (Cognito, Azure AD, etc.), Platform API | AWS ALB (or Azure App Gateway, nginx, etc.) |
| **ECR** | Agent Docker image storage | Fargate (pull on task launch), CI/CD (push on build) | ECR |

## Data flow

### Run submission — the platform is a bookkeeper (ADR-013 §1)
1. Browser POSTs `{workflow_id, policy_id, story_id, github_integration_id, jira_integration_id?, ai_vendor_integration_id}` to Platform API
2. Platform API validates: workflow exists + belongs to project, policy exists + is tied to that workflow, integrations exist, are verified (`verified_at IS NOT NULL`), and belong to the project
3. `source_branch` is not user input — it resolves at creation from the selected GitHub integration's `config.base_branch` (fallback `"main"`); the engine reads the stored value at init
4. Platform API creates the run row (`state=pending`, `run_branch=NULL`) and INSERTs a work item into `work_queue` (action=`start`, payload=`{story_id}`) — everything else the engine reads from the DB
5. Platform API returns 201 + run_id to browser — no HTTP call to Engine
6. An Engine process picks up the work via `SELECT ... FOR UPDATE SKIP LOCKED`, sets `state = 'claimed'`, and spawns a dispatch task (per-run asyncio lock + claim re-assert + sibling supersede guard against double-driving)
7. Browser polls `/api/poll?since=<cursor>` for event stream updates

### Step execution
1. **Engine init (`_init_run`, ADR-013 §2)** — before any launch: load run/workflow/policy/integrations; validate pairing + skills; derive branch `feat/<safe_story>/<DDMMYYYYHHmm>-<first-4-of-run-uuid>`; create it via the GitHub REST API (idempotent on same sha, suffix-bump on different); resolve every step's `model:` tier through the AI-vendor integration's `model_high/medium/low` config keys; persist `run_branch`, `state=running`, and ALL step rows (pending). Init failures classify the run `failed` with zero containers launched.
2. Engine loads step context: allowed result statuses, gate flag, result status meanings, reviewer feedback, upstream hand-off (if any) — never routing targets
3. Engine calls `Runtime.launch()` (DockerRuntime → docker-py) with the ADR-013 §5 env bundle (git coordinates + `RUN_BRANCH`, Jira MCP env, tier-resolved `BB_MODEL` + vendor key, `RUN_ID/STEP_ID/ATTEMPT_NO/SKILL/RESULT_DIR`, context) — secrets resolved fresh per launch from Secure Storage, last-4 fingerprints only
4. Agent container starts: writes diagnostics, clones the **pre-existing** run branch (`run_skill.sh` only creates it when missing), downloads the step's skill bundle via `BB_SKILL_URL` (presigned GET) + verifies `BB_SKILL_SHA256` + extracts into `.claude/skills/<skill>` (overwriting anything the repo tracks there), materializes `BB_CONTEXT` to `/home/node/context.json`, runs Claude Code with `--model <tier> --dangerously-skip-permissions --mcp-config`, commits, pushes
5. Agent writes `bb_step_result.json` to the mounted `/out` result volume (DockerRuntime); Object Storage artifact read/write is the FargateRuntime story (ADR-011, deferred)
6. Container exits; engine polls the runtime for exit status, with progress heartbeats from `progress.json`
7. Reconciler joins the two signals — result payload (from the container, at `/out/bb_step_result.json`) + exit status (from the runtime) → classifies outcome (completed/BLOCK/changes_requested/escalation_required/failed_*)
8. Engine persists step outcome to Postgres (steps + transitions rows) — step completion, routing, and any gate pause commit in ONE transaction; `current_step` always points at the next unrun (or gated) step
9. Engine evaluates policy gate for this step+status → if gate required, **pause the run** (`run.state="paused"`; the gated step row stays `exec_state="completed"`; the gate card is persisted on the `awaiting_approval` transition's JSONB payload) and push an `approval_required` event to the platform
10. If no gate, consult workflow `on:` map → route to next step or DONE

### Context passing between steps

Nothing flows between steps through shared memory or direct calls — each step is one
ephemeral container that receives everything it is allowed to know at launch, and leaves
everything downstream needs in durable places. Five channels carry context, split by who
owns the information:

| Channel | Owner | Medium | What it carries |
|---------|-------|--------|-----------------|
| **Run branch** | Agent | git (committed + pushed) | The work product itself: code, design docs, report files like `verification.md` |
| **Result payload** | Agent | `/out/bb_step_result.json` (host dir per run/step/attempt, rw mount) | The verdict word (`completed` / `BLOCK` / `changes_requested` / `escalation_required` / `failed_*`), `summary`, curated `review_files` (from `BB_REVIEW:` lines), all `files`, `commit` sha |
| **Step context** | Engine | `BB_CONTEXT` env → written by the runner to `/home/node/context.json` (no mount) | `allowed_result_statuses`, `result_status_meanings`, `gate_follows` / `gate_role`, `advice`, `reviewer_feedback`, `upstream_handoff` — the skill's vocabulary and audience, **never routing targets** |
| **Handoff** | Engine | inside `upstream_handoff` of the context file | The prior step's non-happy verdict: `{from_step, status, summary, report_files}` |
| **Reviewer feedback** | Human | inside `reviewer_feedback` of the context file | The send-back comment the re-run must address |

**The handoff** (`_handoff_for`, `engine_service/state_machine.py`). When step N finishes with
a non-`completed` verdict that routes *forward* (e.g. `test-verify` → `BLOCK` → `implement`),
the engine builds `{from_step, status, summary, report_files}` and injects it into the next
step's context. `build_step_context` applies a self-loop guard: a step never receives its own
verdict back as a handoff. The runner renders the handoff as a preamble line of the next
agent's prompt — "Read its report first: `docs/verification.md`". The report files exist
because the prior step committed them to the run branch before its verdict counted
(push-lands-or-retry). Handing a verdict forward *with its evidence* is what makes verdict
loops converge instead of spinning blindly. The key is `report_files` on both sides — it must
match `run_skill.sh`'s jq path `.upstream_handoff.report_files`, or the file-list clause is
silently dropped from the rendered prompt (the integration test
`test_block_verdict_hands_off_to_next_step` pins this contract).

**Reviewer feedback** (ADR-007). A send-back decision carries the reviewer's comment in the
`continue` work-item payload; `_apply_send_back` resets every step after the target (plus the
gated step itself when it is the target), points `current_step` at the target, and the comment
becomes `reviewer_feedback` in the re-run's context — rendered as "A reviewer sent your
previous attempt back for revision … Address this specifically."

**Crash durability of the handoff.** The handoff dict is in-memory per dispatch — it is
*derived* data, reconstructible from what IS persisted, so it is never itself stored. Three
crash shapes:

1. **Paused at a gate** (the common handoff source). The gate card — summary, artifact, files,
   `review_files`, `next_hint` — is persisted on the `awaiting_approval` transition's JSONB
   payload. A crash mid-pause is healed by ADR-003: the recovery top-up token
   (`continue{action:resume}`) merely re-notifies the open gate, nothing re-runs. When the
   human later approves, `_apply_approve` rebuilds the outcome from the persisted card and
   re-derives the handoff via `_handoff_for` — nothing is lost.
2. **Crash after step N committed its completion but before step N+1 launched.** The
   completion, the full outcome payload (`summary` / `files` / `review_files` / `commit` on
   the transition row), and `run.current_step = target` commit in ONE transaction. On resume
   the dispatch starts at the persisted `current_step`. The handoff *preamble* is not
   re-injected on this path (the in-memory dict is gone), but the verdict and summary remain
   in the transitions table and the report files remain committed on the branch — the
   evidence survives even where the message does not. The transition payload holds everything
   needed if this is ever upgraded to rebuild it.
3. **Crash mid-step.** ADR-003 re-attaches the surviving container (same attempt, remaining
   deadline) or relaunches the same `attempt_no` — push-lands-or-retry makes the double-run
   safe. The handoff is built only after the outcome is persisted, so this path loses nothing.

**The exact prompt the next agent sees.** `run_skill.sh` composes one prompt per container
invocation (Claude Code `-p`, non-interactive). The template:

```text
Run the ${SKILL} skill. Follow ${WORKDIR_REPO}/.claude/skills/${SKILL}/SKILL.md exactly.
${STORY_LINE}            # when STORY_ID is set
${HANDOFF_LINE}          # when upstream_handoff is present
${FEEDBACK_LINE}         # when a reviewer sent the step back
When you are completely finished, judge the outcome of your own execution and end your
reply with a final line in exactly this form:
BB_OUTCOME: <one of ${ALLOWED}>

What each outcome means:
${MEANINGS}              # one "  - status: meaning" line per ALLOWED status (filtered)
Choose the one that honestly describes your execution. Produce the artifacts your skill calls
for first; the outcome word is your verdict on that work.

Also tell the reviewer which files are worth looking at. For each file a human should review,
add a line (before BB_OUTCOME) in exactly this form:
BB_REVIEW: <path relative to repo root> | <short reason>
List every file that matters to the review — the key source you wrote or changed, the report
to check against — not incidental or generated files. If your skill already records this in its
own hand-off doc, mirror the same set here. These become the reviewer's default file list.
Do not create or modify any file for the purpose of reporting that outcome — the line in your
reply is the only thing that is read.
${GATE_SENTENCE}         # when gate_follows=true: "A human reviewer will read your summary
                         # before the run continues — write your closing summary for them."
```

The three variable blocks expand to:

```text
STORY_LINE    = "The target story is ${STORY_ID}. Use the Atlassian (Jira) MCP to fetch its
                details (summary, description, acceptance criteria) before you begin."
HANDOFF_LINE  = "You are being run because the '${from_step}' step returned '${status}',
                which routes here to be addressed. Read its report first: ${report_files}
                (in the repo). Address every point it raises. Its summary: ${summary}"
FEEDBACK_LINE = "A reviewer sent your previous attempt back for revision. Their feedback:
                ---
                ${comment}
                ---
                Address this specifically."
```

A concrete instance — `implement` reached by a `test-verify` BLOCK on story BB-42 — differs
from the template only in its variable parts:

```text
Run the implement skill. Follow /workspace/repo/.claude/skills/implement/SKILL.md exactly.
The target story is BB-42. Use the Atlassian (Jira) MCP to fetch its details
(summary, description, acceptance criteria) before you begin.
You are being run because the 'test-verify' step returned 'BLOCK',
which routes here to be addressed. Read its report first: docs/verification.md (in the repo). Address every point it raises.
Its summary: 3 of 12 tests fail — POST /register returns 500 on valid input.
```

…followed by the boilerplate above with `BB_OUTCOME: <one of
["completed","changes_requested","escalation_required"]>` and the three matching meaning
lines. The wrapper owns the control-plane result: the runner parses `BB_OUTCOME:` /
`BB_REVIEW:` lines from the agent's final reply — the prompt deliberately never names a
result file (an earlier version did, and the agent's own `result.json` collided with the
skills' artifacts).

### Approval flow — decisions are queued dispatch tokens
1. Browser polls, sees the run paused → UI shows gate card with review files + summary
2. Reviewer clicks Approve or Request Changes (optionally with a comment). The reviewer's
   project membership role (from `memberships`) is checked against the policy gate's role requirement.
3. Browser POSTs `/api/runs/{id}/decision` with `{action: approve|send_back, send_back_to, comment}`
4. Platform API validates `run.state == "paused"` (else 409), validates `send_back_to` against
   the workflow's step ids, and INSERTs a work item into `work_queue`
   (action=`continue`, payload=`{action, send_back_to, comment, actor}`) — **no state mutation**; the UI re-poll sees the flip when the engine acts
5. An Engine process claims the work item and applies the decision (audited as an
   `awaiting_approval → completed` transition with the reviewer as `actor`):
   - **Approve + route→next_step**: launch the next step in the workflow per the `on:` map.
   - **Approve + route→DONE**: complete the run.
   - **Request changes**: reset every step after the named `send_back_to` target (plus the
     gated step itself when it is the target), point `current_step` at the target, and re-run
     it with the reviewer's comment injected via the `reviewer_feedback` field in step context
     (ADR-007). The review record and older attempt history are preserved. This is
     deterministic — no workflow configuration required.
6. Engine pushes the transition event to Platform API (`/webhooks/engine`) for the event stream

### Engine crash recovery

The work queue uses a **claim + heartbeat** pattern (ADR-003). Work items are never deleted
on pickup — they transition `pending → claimed → done`. If an Engine process dies, its
heartbeat stops and the work item remains in `claimed` state. On restart, the recovery pass
converges the two sources of truth (`work_queue` and `runs`) and resumes execution.

```
Engine-1 (dies)          Runtime             Postgres               Engine-1 (restart)
    │                       │                    │                       │
    │  💥 CRASH             │                    │                       │
    │                       │  container keeps   │  work_queue:          │
    │                       │  running           │  Run-A: claimed       │
    │                       │                    │  heartbeat stale       │
    │                       │                    │  runs: Run-A=running  │
    │                       │                    │                       │
    │                       │                    │     recover_on_startup()
    │                       │                    │◄──────────────────────│
    │                       │                    │                       │
    │                       │                    │  UPDATE work_queue     │
    │                       │                    │  SET state='pending'   │
    │                       │                    │  WHERE state=claimed   │
    │                       │                    │  AND heartbeat stale   │
    │                       │                    │                       │
    │                       │                    │  FIND runs WHERE       │
    │                       │                    │  state IN (running,    │
    │                       │                    │  paused) AND no        │
    │                       │                    │  pending/claimed item  │
    │                       │                    │                       │
    │                       │                    │  INSERT work_queue      │
    │                       │                    │  action=continue,       │
    │                       │                    │  payload={action:resume}│
    │                       │                    │───────────────────────►│
    │                       │                    │                       │
    │   (resume dispatch claims the item)        │                       │
    │                       │                    │                       │
    │  status() on stored  │                    │                       │
    │  fargate_task_arn    │                    │                       │
    │◄─────────────────────│                    │                       │
    │  → gone              │                    │                       │
    │                       │                    │  relaunch same         │
    │                       │                    │  attempt_no — push-    │
    │                       │                    │  lands-or-retry makes  │
    │                       │                    │  double-run safe       │
    │                       │                    │───────────────────────►│
```

**Recovery pass** (`recover_on_startup()`, called before `worker_loop()`):

1. **Reclaim orphaned queue items:** Query `work_queue` for `state = 'claimed'` AND
   `heartbeat_at < NOW() - 60s`. Reset them to `state = 'pending'` — they re-enter the
   worker loop naturally (a re-claimed `start` item is safe: `_init_run` is idempotent —
   branch exists with same sha → skip creation; step rows exist → skip re-insert).
2. **Re-enqueue in-flight runs:** Query `runs` for `state IN ('running', 'paused')` with
   **no** pending or claimed work item. For each, INSERT a `work_queue` item
   (`action=continue`, payload `{action: resume}`). When the engine claims it, the
   dispatch resumes **from persisted state** — it never replays history:
   - **Run `paused`:** re-notify and wait for a decision again.
   - **Run `running`, current step `exec_state='running'`:** best-effort re-attach via the
     runtime handle stored in `steps.fargate_task_arn` (container id under DockerRuntime).
     Handle alive → keep polling with the remaining deadline; gone → relaunch the **same
     `attempt_no`** (push-lands-or-retry makes a double-run safe — only the second push
     counts). Steps that never reached `running` are simply launched.

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
| **Runtime backend** | `DockerRuntime` (docker-py, local dev) — `FargateRuntime` deferred | `FargateRuntime` (boto3 `run_task`/`describe_tasks`) for prod, or `KubernetesRuntime`/`LambdaRuntime` behind the same protocol (ADR-013 §4) |
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
| Execution — detailed results | Result volume: host-mount `/out` per container under `DockerRuntime` (agent logs, diagnostics, `bb_step_result.json`, summaries); Object Storage artifacts + pre-signed URLs are the deferred `FargateRuntime` story (ADR-011) |
| Approval & feedback flows | Gate cards in UI; `/runs/{id}/decision` enqueues a `continue` work item (validates `state=paused`, no state mutation); `send_back` rewinds to the named `send_back_to` step per ADR-007 |
| Fargate integration | Deferred — `FargateRuntime` behind the existing Runtime protocol; `steps.fargate_task_arn` is reused today as the generic runtime handle (container id), which crash recovery re-attaches to |
| Per-run budget cap | `cost_usd` columns on runs/steps; cap enforcement (designed, build deferred) |
| Async run dispatch | `work_queue` table (ADR-003); Platform API writes, Engine pulls via SKIP LOCKED; claim+heartbeat for crash recovery |
