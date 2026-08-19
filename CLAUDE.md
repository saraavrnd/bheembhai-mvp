# CLAUDE.md

## Project overview

**BheemBhai** is a governed, containerized pipeline for product-development skills — a backend that orchestrates a library of AI-powered skills as an assembly line, with human-in-the-loop gates where policy demands approval.

The platform turns a fixed sequence of skills (story-design → test-creator → implement → test-verify → code-review → pr-create) into **per-project configuration**: workflows define what runs and routing, policies gate where a human intervenes, and feedback is wired where the project wants it.

## Architecture (two services + agent containers)

1. **Platform API** (`platform_api/`) — FastAPI server that serves the browser and manages users/projects/integrations/workflows/policies. It is a **bookkeeper** (ADR-013 §1): `POST /api/runs` validates + creates the run row + enqueues a `work_queue` item; it never mutates run state after that — gate decisions are queued as `continue` items, not applied.

2. **Engine Service** (`engine_service/`) — The orchestrator. A long-lived asyncio process that claims work from `work_queue` (SKIP LOCKED), initializes runs (`_init_run`: branch creation via GitHub REST, model resolution), runs the workflow state machine, launches one container per step behind a **Runtime protocol** (`DockerRuntime` today; `FargateRuntime` deferred), reconciles results against exit codes, applies policy gates, enforces model/cost/loop limits. The agent is a *dumb worker*; the backend decides routing. `shared/bheembhai/` holds the SQLAlchemy models + config used by both services.

3. **Agent container** (`agent/`) — A single Docker image (`node:20-slim` + Claude Code CLI + git + jq + uvx) that is a **pure runtime: no skills baked in**. Each invocation runs `run_skill.sh` against ONE skill: it clones/checks-out the run branch (git mode), downloads the step's skill bundle from S3 (`BB_SKILL_URL` presigned GET + `BB_SKILL_SHA256` verify) and extracts it into `.claude/skills/<skill>` — unconditionally overwriting anything the repo tracks there — materializes `BB_CONTEXT` to `/home/node/context.json`, runs Claude Code with `--dangerously-skip-permissions`, commits + pushes, and ships the structured `bb_step_result.json` (plus progress.json / agent.log / diagnostics.txt) to Object Storage via per-launch presigned PUT URLs — the container has **zero host mounts** (ADR-014).

4. **Web UI** (`platform_api/templates/` + `static/`) — server-rendered + polling UI. Shows the live pipeline, step status, artifacts, gate cards, and cost.

> **Legacy R&D:** `engine.py` (~873 lines), `app.py`, and `static/index.html` remain at the
> repo root as stable reference material for the state machine and event flow. The production
> two-service implementation lives in `platform_api/` and `engine_service/`; the legacy files
> are not part of the production path.

## Key source files

| File | Purpose |
|------|---------|
| `platform_api/` | Platform API (FastAPI + Jinja2): auth, project/integration/workflow/policy CRUD, run submission (`routers/runs.py` — validates + enqueues only), engine webhook receiver (`routers/webhooks.py`), `submit_decision` → continue item |
| `engine_service/` | Engine Service: `worker.py` (claim loop + per-run-lock dispatch), `run_init.py` (ADR-013 §2 `_init_run`), `state_machine.py` (`RunDriver`), `runtime.py` (Runtime protocol + `DockerRuntime` + reconcile), `workflow.py` (YAML parse/validate/tier resolve), `contexts.py` (step context + env bundle), `notifier.py` (engine→platform webhooks), `recovery.py` (2-step crash recovery) |
| `shared/bheembhai/` | SQLAlchemy models (`Run`, `Step`, `Transition` with JSONB `payload`, `WorkQueueItem`, integrations), `AppConfig`/`EngineConfig`, DB init — shared by both services |
| `agent/run_skill.sh` | Skill runner (~527 lines): git clone/branch management, skill-bundle delivery (`BB_SKILL_URL` curl + sha256 verify + path-safety check + extract into `.claude/skills/<skill>`), MCP config injection (Jira + GitHub tokens), context materialization (`BB_CONTEXT` → `CONTEXT_FILE`), diagnostics, Claude Code invocation with model enforcement, commit/push, ADR-014 upload channels (heartbeat + EXIT trap PUTs under `BB_RESULT_PUT_URL` / `BB_PROGRESS_PUT_URL` / `BB_LOG_PUT_URL` / `BB_DIAG_PUT_URL`), result extraction from `BB_OUTCOME:` / `BB_REVIEW:` lines |
| `agent/Dockerfile` | Agent image (pure runtime): node:20-slim, installs git/python3/jq/curl/uv, Claude Code CLI, MCP template only — no skills baked in, runs as non-root `node` user; image-owned `/workspace` + `/out` (no host mounts) |
| `agent/mcp.json` | MCP config template with `${JIRA_URL}`, `${JIRA_USERNAME}`, `${JIRA_API_TOKEN}`, `${GH_TOKEN}` placeholders — substituted at runtime by `run_skill.sh` |
| `tests/unit/`, `tests/integration/` | Test suite: pure-logic unit tests (no DB) + integration tests against compose Postgres with a scripted `FakeRuntime` behind the Runtime protocol; select with `pytest -m unit` / `pytest -m integration` |
| `engine.py`, `app.py`, `static/index.html` (root) | Legacy R&D engine + UI — reference only, superseded by the two-service structure |

## Configuration files

| File | Purpose |
|------|---------|
| `config/workflow-story-delivery.yaml` | Default workflow: 7 steps (story-design through pr-create), each with skill name, model tier, deadline, and `on:` routing map |
| `config/policy-strict.yaml` | Humans approve at story-design, code-review, pr-create |
| `config/policy-governed.yaml` | Like strict, plus gates on non-happy outcomes (BLOCK at test-verify) |
| `config/policy-fast.yaml` | Fully autonomous — no gates |
| `config/profiles/anthropic.env` | Legacy R&D profile: tier names map to real Anthropic models |
| `config/profiles/deepseek.env` | Legacy R&D DeepSeek profile: tiers → `deepseek-v4-pro`/`deepseek-v4-flash` |

Production tier → model mapping lives on each project's **AI-vendor integration**
(`project_integrations.config.model_high/medium/low`) — the `config/` files are legacy R&D
seed material.

## Skill library (delivered per step from S3)

Each skill is a catalog row (`skills` + `skill_files`) whose content is a directory of
`SKILL.md` (the skill definition), `references/`, `templates/`, and `examples/`. The agent
image contains **no skills**. On every content-changing save the platform packs the skill
into a deterministic tar.gz and PUTs it to Object Storage under the content-addressed key
`skills/<name>/<sha256>.tar.gz`, stamped on the row (`s3_key`/`sha256`). The engine freezes
the key onto each step row at run init (self-healing unexported skills on the spot) and
signs a fresh presigned GET per launch → `BB_SKILL_URL` + `BB_SKILL_SHA256` env;
`run_skill.sh` downloads, verifies, and extracts it into `.claude/skills/<skill>` —
BheemBhai is authoritative over anything the repo tracks there.

The seeded skill catalog:

- **story-design** — Design a story from a Jira ticket
- **tech-design** — Revisit architecture decisions
- **test-creator** — Write tests before implementation
- **implement** — Build the feature
- **test-verify** — Verify tests pass (honest-green check)
- **code-review** — Review code against a rubric + security checklist
- **pr-create** — Open a pull request
- **story-implement** — Orchestrates the full story-delivery loop end-to-end
- **design-sync** — Sync components to Claude Design projects
- **epic-sequence** — Sequence stories within an epic
- **prd** / **prd-decompose** — PRD creation and decomposition
- **project-scaffold** — Scaffold new projects
- **user-story** — Create user stories
- **revert-run** — Revert a prior skill run
- **simplify** — Code simplification and cleanup

## Tech stack

- **Backend**: Python 3, FastAPI, uvicorn, PyYAML, SQLAlchemy 2 (async) + asyncpg, PostgreSQL, Docker SDK for Python (legacy `engine.py` uses raw SQL + SQLite)
- **Agent container**: Node.js 20-slim, Claude Code CLI (`@anthropic-ai/claude-code`), bash, git, jq, uv/uvx
- **Frontend**: Jinja2 templates + vanilla JS, polling-based event stream, Space Grotesk + Inter + JetBrains Mono fonts
- **MCP servers**: Atlassian (Jira via `mcp-atlassian`), GitHub (via `@modelcontextprotocol/server-github`)
- **Target deployment**: Internal tool on single EC2 + local Docker

## Key design principles

- **Dispatch tokens, not RPC**: the platform enqueues `work_queue` items (`start` / `continue` / `cancel`) and the engine claims them via `SELECT ... FOR UPDATE SKIP LOCKED`. One dispatch advances a run exactly one pause — a gate or a terminal state — then the item goes `done`. **Gate pause is `runs.state = "paused"`** (the gated step row stays `exec_state = "completed"`; the gate card lives in the `awaiting_approval` transition's JSONB `payload`). Decisions are `continue` items (`{action: approve | send_back | resume, send_back_to, comment, actor}`) — the platform never mutates run state directly. Terminal run states are `completed` / `failed` / `cancelled`.
- **Backend-authoritative routing**: A skill's `next` hint in its result is advisory; the workflow's `on:` map is the authority. `route_to` lets a step name its own target, otherwise `on:` decides.
- **One ephemeral container per step/attempt** — isolation first.
- **Two signals on separate channels**: the result payload (agent-uploaded to Object Storage at `results/<run>/<slug step>/<attempt>/bb_step_result.json` — read back by the engine) and the exit status (polled from the runtime via `Runtime.status()`). A reconciler joins them.
- **Three separable overlays**: workflow (what runs + routing), policy (where a human gates), notifications (who's told). Removing a rule that changes *which steps run* → workflow; only changes *where a human intervenes* → policy.
- **Push-lands-or-retry invariant**: A step only "counts" once its git push succeeds. Crashes before push → work lost, retry from last pushed state. The branch always reflects exactly completed steps.
- **Platform plumbing excluded from commits**: `.claude/`, `.mcp.json`, `.gitignore` are filtered from git commits so agent infrastructure never lands in the user's branch.

## Result statuses (fixed enum — engine-owned)

| Status | Meaning |
|--------|---------|
| `completed` | Skill ran fine, verdict is positive — proceed |
| `BLOCK` | Skill ran fine but found a real problem (e.g., tests aren't honestly green) |
| `changes_requested` | Work produced, but the skill/reviewer wants revisions |
| `escalation_required` | Something outside the skill's authority |
| `failed_execution` | Deterministic failure (bad input, auth) |
| `failed_infra` | Transient infrastructure failure — retried |
| `failed_timeout` | Step exceeded its deadline — retried |
| `failed_incomplete` | Container exited without publishing a result — retried |

The domain statuses (`completed`, `BLOCK`, `changes_requested`, `escalation_required`) presuppose the skill ran fine; they differ in *verdict*. The `failed_*` family means "couldn't run."

## Workflow YAML structure

```yaml
workflow: story-delivery
version: 1
start: story-design       # first step id
steps:
  - id: story-design
    skill: story-design   # must match a skill name in the catalog (skills table)
    model: high           # per-step model tier (high/medium/low), backend-enforced via --model
    label: Design the story  # human-readable
    deadline: 900           # seconds
    "on":                   # routing map: result status → next step id (or DONE)
      completed: test-creator
      changes_requested: story-design   # self-loop
      escalation_required: tech-design
```

Note: `"on"` must be quoted in YAML (bare `on` parses as boolean `true` under YAML 1.1).

## Per-step model enforcement

Workflows name model **tiers** (`high` / `medium` / `low`). The engine resolves tier → concrete
model id at `_init_run` from the run's selected **AI-vendor integration** flat config keys
(`model_high` / `model_medium` / `model_low`) — the mapping lives on the integration
(ADR-013 §2; a `model_profiles` indirection table is explicitly deferred).
`steps.model_requested` stores the resolved concrete id; `BB_ALLOWED_MODELS` for the run =
those three values; the backend passes `--model` to Claude Code for enforcement. A missing
tier mapping fails init with `failed_execution` — zero containers launched. Model usage is
recorded in diagnostics so "did the right model run?" is always answerable.

AI-vendor integration `config` carries non-secret values only; API tokens come from Secure
Storage via `credential_ref`, resolved fresh per launch (ADR-012). Vendor key rule:
`claude` → `ANTHROPIC_API_KEY`; other vendors → `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`.

## Git mode

**Engine-owned branches (ADR-013 §2):** `runs.run_branch` is nullable and derived + persisted
by the engine at `_init_run` — never by the platform. The engine creates the branch via the
GitHub REST API (idempotent: ref exists with the same sha → proceed; different sha → suffix
bump and retry once) using the GH_TOKEN resolved from the run's selected GitHub integration.

Default (`BB_GIT_MODE=1`): each run operates on a branch cut from the integration's
`config.base_branch` (fallback `main`), named
`feat/<safe_story>/<DDMMYYYYHHmm>-<first-4-of-run-uuid>`. Steps run sequentially on the same
branch — no concurrent writers, no merge handling needed. The agent clones the
**pre-existing** run branch (`run_skill.sh` only creates one when missing). Each step commits
+ pushes; work only "counts" once pushed.

Copy mode (`BB_GIT_MODE=0`): uses `BB_SEED_REPO` as a local dir copy, never pushes. For demos without a remote.

## Guardrails

- **Per-step visit cap** (`BB_MAX_STEP_VISITS`, default 3): breaks runaway loops. A step returning the same non-happy verdict repeatedly is halted and escalated.
- **Fresh-launch channel hygiene**: a step launch clears its attempt's S3 channel keys (result/progress/logs) first — attempt numbers are reused across visits and retries, and a stale result object at the key would otherwise replay the previous visit's verdict (run 07c4b440 recorded visit 1's payload byte-for-byte as visit 2's result).
- **Per-step model enforcement**: ensures the intended (often cheaper) model actually runs.
- **Push-lands-or-retry**: failures retry from last good state.
- **Fail-fast init**: bad git credentials, missing integrations, unknown skills, or missing tier mappings surface at `_init_run` as classified run failures (failed_execution/failed_infra) — before any container minutes are spent.
- **Crash recovery (ADR-003, 2 steps)**: (1) stale `claimed` queue items reset to `pending` (idempotent `_init_run` makes re-claiming a `start` safe); (2) runs in `(running, paused)` with no pending/claimed item get a `continue{action: resume}` item — the dispatch resumes from persisted state and re-attaches via the runtime handle in `steps.fargate_task_arn` (alive → keep polling with the remaining deadline; gone → relaunch the same `attempt_no`).

## API endpoints

Production surface (platform API on host :9000, engine on host :9001, Postgres on host :5555 — see `docker-compose.yml`):

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI (server-rendered, polling) |
| POST | `/api/runs` | Start a run — body: `{project_id, workflow_id, policy_id, story_id, github_integration_id, jira_integration_id?, ai_vendor_integration_id}` (ADR-013 §1). Validates + creates the run row + enqueues a `start` work item |
| POST | `/api/runs/{id}/decision` | Queue a gate decision (409 unless `run.state == "paused"`): body `{action: approve \| send_back, send_back_to?, comment?}` → INSERTs a `continue` work item, **no state mutation** |
| POST | `/api/runs/{id}/cancel` | Stop a run (404 unknown, 409 already terminal): enqueues a `cancel` work_queue token, **no state mutation**. The engine claims it outside the per-run lock, signals the in-flight dispatch (in-memory event → reconciler aborts within one poll tick, container force-removed), voids queued siblings, closes any open gate, and records `runs.state = "cancelled"` |
| GET | `/api/runs/{id}` | Run state |
| GET | `/api/runs/{id}/file?path=` | Read an artifact from git at the step's recorded commit SHA, falling back to demo stubs (2 MB cap, text-only) |
| GET | `/api/poll?since=<cursor>` | Poll event stream (primary transport, cursor-based) |
| POST | `/webhooks/engine` | Engine→platform event receiver (validates `X-BB-Secret`, 202, no state written) |
| GET | `/health` | Engine health: `queue_depth`, `orphaned_items`, active dispatches |

Legacy R&D API (`app.py`, root — same `/api/runs` shape with `{workflow, policy, story_id, source_branch}` bodies) is reference only.

## Running the project

```bash
# Compose: platform API (:9000) + engine service (:9001) + Postgres (:5555)
docker-compose up --build

# First boot of a FRESH database only (one-off): seed skills + workflows from
# disk. Seeding OVERWRITES DB catalogs — never run it on a live instance with
# user edits; the default is OFF.
BB_SEED_ON_STARTUP=true docker-compose up

# Build the agent image (engine launches it via BB_AGENT_IMAGE)
docker build -t bheembhai/agent:latest agent/
```

```bash
# Legacy R&D single-server path (still works for demos):
export ANTHROPIC_API_KEY=sk-...
export BB_AGENT_IMAGE=bheembhai/agent:latest
export GIT_REMOTE_URL=https://github.com/you/your-repo.git
export GIT_SOURCE_BRANCH=main
export GH_TOKEN=ghp_...
export JIRA_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...
export BB_ALLOWED_MODELS="claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5"
uvicorn app:app --port 8000
```

## Testing

- `pytest -m unit` — pure-logic tests (no DB, no Docker): YAML parse + validation, tier
  resolution, context purity, `route_next`/`backtrack`, reconcile classification, notifier.
- `pytest -m integration` — needs compose Postgres up
  (`postgresql+asyncpg://bheembhai-mvp:...@localhost:5555/bheembhai_test`); a scripted
  `FakeRuntime` stands in for the Runtime protocol; JSONB rules out SQLite for this layer.
- Tests live under `tests/unit/` and `tests/integration/`, marked by path via a
  `pytest_collection_modifyitems` hook in `tests/conftest.py`. A root `conftest.py`
  `collect_ignore`s the dev scratch scripts `quick_test.py` / `main.py` (they match pytest's
  `*_test.py` glob and mutate `os.environ` at import).


## Coding conventions

- **Python**: SQLAlchemy 2 async models in `shared/bheembhai/models/` (`Run`, `Step`, `Transition` with JSONB `payload`, `WorkQueueItem`), dataclass-driven engine domain (`WorkflowSpec`, `PolicySpec`, `Handle`), class constants for enums (`ExecState`, `Result`), asyncio worker with a `sessionmaker()` session per dispatch task (the SKIP LOCKED claim loop never blocks on a multi-minute run)
- **Bash**: `set -uo pipefail`, defensive JSON construction via `jq`, atomic writes (write `.tmp` then `mv`), heartbeat progress loop for liveness
- **Containers**: run as non-root `node` user (required for `--dangerously-skip-permissions`), zero host mounts (ADR-014) — the image owns `/workspace` and `/out`
- **Step channels (Object Storage)** (ADR-014): the agent uploads `bb_step_result.json` (CRITICAL — PUT failure rewrites the verdict to `failed_infra` and exits 4, retried), `progress.json` (heartbeat, 5s), `agent.log` (heartbeat + exit), `diagnostics.txt` (exit) via per-launch presigned PUT URLs under deterministic keys `results/<run>/<slug step>/<attempt>/…` and `logs/<run>/<slug step>/<attempt>/…`; the engine uploads `container.log` from the docker API and reads everything back from the store. A missing URL just skips that upload — never a failure. LocalStorage offers no PUT URLs (host-run unit tests only); real step containers need S3
- **Error handling**: auth failures classified by *which* credential (model 401 vs Jira vs git), diagnostics written before agent runs so they survive container deletion
- **Result extraction**: agent emits `BB_OUTCOME: <status>` and `BB_REVIEW: <path> | <note>` lines in its final reply — the runner script parses these, never names a result file in the agent prompt (that caused collision with skills' own `result.json`)
- **Credentials**: last-4-character fingerprint in diagnostics for verification without exposure, token embedded in git URL only during clone/push then scrubbed

## Important patterns

- **Failure hand-off**: When a step routes to another on a non-happy verdict, the next step's prompt includes the prior step's status, summary, and report files — making loops converge instead of spinning blindly.
- **Review file curation**: Skills declare `BB_REVIEW:` lines; the UI shows these curated files by default (with a toggle for all changed files). If none emitted, falls back to full git diff.
- **Context injection**: The backend builds per-step context with `{allowed_result_statuses, gate_follows, result_status_meanings, reviewer_feedback, upstream_handoff}` — the skill sees only its vocabulary, never routing targets. Full channel breakdown (run branch, result payload, context file, handoff, reviewer feedback), handoff crash-durability, and the exact agent prompt: `docs/architecture.md` §Context passing between steps.
- **Validation at run-submit time**: workflow/policy pairing validated before any container launches (HTTP 422 on mismatch), boot-time config checked with `BB_STRICT_STARTUP` option.
