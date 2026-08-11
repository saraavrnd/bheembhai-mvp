# CLAUDE.md

## Project overview

**BheemBhai** is a governed, containerized pipeline for product-development skills — a backend that orchestrates a library of AI-powered skills as an assembly line, with human-in-the-loop gates where policy demands approval.

The platform turns a fixed sequence of skills (story-design → test-creator → implement → test-verify → code-review → pr-create) into **per-project configuration**: workflows define what runs and routing, policies gate where a human intervenes, and feedback is wired where the project wants it.

## Architecture (3 parts)

1. **Backend engine** (`engine.py`, ~873 lines) — The orchestrator. Runs a workflow state machine, launches one Docker container per step, reconciles results against exit codes, applies policy gates, enforces model/cost/loop limits. The agent is a *dumb worker*; the backend decides routing.

2. **Agent container** (`agent/`) — A single Docker image (`node:20-slim` + Claude Code CLI + git + jq + uvx). Each invocation runs `run_skill.sh` against ONE skill: it clones/checks-out the run branch (git mode), runs Claude Code with `--dangerously-skip-permissions`, commits + pushes, and publishes a structured `bb_step_result.json` to the mounted `/out` directory.

3. **Web UI + API** (`app.py`, `static/index.html`) — FastAPI backend with a polling-based single-file UI. Shows the live pipeline, step status, artifacts, gate cards, and cost.

## Key source files

| File | Purpose |
|------|---------|
| `engine.py` | Core engine: `Run`, `Workflow`, `Policy`, `DockerRuntime`, `EventBus`, SQLite schema, model profile resolution, startup validation |
| `app.py` | FastAPI app: `.env` loader, REST API endpoints (`/api/runs`, `/api/runs/{id}/decision`, `/api/poll`, `/api/config`), mounts static UI |
| `agent/run_skill.sh` | Skill runner (~527 lines): git clone/branch management, MCP config injection (Jira + GitHub tokens), diagnostics, Claude Code invocation with model enforcement, commit/push, result extraction from `BB_OUTCOME:` / `BB_REVIEW:` lines |
| `agent/Dockerfile` | Agent image: node:20-slim, installs git/python3/jq/curl/uv, Claude Code CLI, copies skills + MCP template, runs as non-root `node` user |
| `agent/mcp.json` | MCP config template with `${JIRA_URL}`, `${JIRA_USERNAME}`, `${JIRA_API_TOKEN}`, `${GH_TOKEN}` placeholders — substituted at runtime by `run_skill.sh` |
| `static/index.html` | Single-file web UI (~550 lines): polling-based, shows pipeline tracker, gate cards with file viewer, event feed |
| `test_engine.py` | 9 tests using `FakeRuntime` (no Docker needed): happy path, changes_requested retry, silent container retry, crash escalation, BLOCK routing, context injection purity, out-of-vocabulary status rejection, ignored next-hint recording |

## Configuration files

| File | Purpose |
|------|---------|
| `config/workflow-story-delivery.yaml` | Default workflow: 7 steps (story-design through pr-create), each with skill name, model tier, deadline, and `on:` routing map |
| `config/policy-strict.yaml` | Humans approve at story-design, code-review, pr-create |
| `config/policy-governed.yaml` | Like strict, plus gates on non-happy outcomes (BLOCK at test-verify) |
| `config/policy-fast.yaml` | Fully autonomous — no gates |
| `config/profiles/anthropic.env` | Default profile: tier names map to real Anthropic models |
| `config/profiles/deepseek.env` | DeepSeek profile: tiers → `deepseek-v4-pro`/`deepseek-v4-flash` |

## Installed skills (agent/skills/)

Each skill is a directory with `SKILL.md` (the skill definition), `references/`, `templates/`, and `examples/`:

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

- **Backend**: Python 3, FastAPI, uvicorn, PyYAML, SQLite, Docker SDK for Python
- **Agent container**: Node.js 20-slim, Claude Code CLI (`@anthropic-ai/claude-code`), bash, git, jq, uv/uvx
- **Frontend**: Vanilla HTML/CSS/JS, polling-based SSE alternative (poll endpoint), Space Grotesk + Inter + JetBrains Mono fonts
- **MCP servers**: Atlassian (Jira via `mcp-atlassian`), GitHub (via `@modelcontextprotocol/server-github`)
- **Target deployment**: Internal tool on single EC2 + local Docker

## Key design principles

- **Backend-authoritative routing**: A skill's `next` hint in its result is advisory; the workflow's `on:` map is the authority. `route_to` lets a step name its own target, otherwise `on:` decides.
- **One ephemeral container per step/attempt** — isolation first.
- **Two signals on separate channels**: the result payload (from container, at `/out/bb_step_result.json`) and the exit status (polled from Docker runtime via `Runtime.status()`). A reconciler joins them.
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
    skill: story-design   # must match a directory in agent/skills/
    model: claude-opus-4-8  # per-step model tier, backend-enforced via --model
    label: Design the story  # human-readable
    deadline: 900           # seconds
    "on":                   # routing map: result status → next step id (or DONE)
      completed: test-creator
      changes_requested: story-design   # self-loop
      escalation_required: tech-design
```

Note: `"on"` must be quoted in YAML (bare `on` parses as boolean `true` under YAML 1.1).

## Per-step model enforcement

Workflows name model **tiers** (`claude-opus-4-8`, `claude-sonnet-4-6`, `claude-haiku-4-5`). A **profile file** (`config/profiles/<name>.env`) maps tiers to real model IDs per vendor. The backend passes `--model` to Claude Code for enforcement. Model usage is recorded in diagnostics so "did the right model run?" is always answerable.

Profiles ship non-secret config only. Auth tokens come from the environment (or AWS Secrets Manager in production).

## Git mode

Default (`BB_GIT_MODE=1`): each run operates on a branch cut from `GIT_SOURCE_BRANCH` (default `main`), named `feat/<story>/<DDMMYYYYHHmm>`. Steps run sequentially on the same branch — no concurrent writers, no merge handling needed. Each step commits + pushes; work only "counts" once pushed.

Copy mode (`BB_GIT_MODE=0`): uses `BB_SEED_REPO` as a local dir copy, never pushes. For demos without a remote.

## Guardrails

- **Per-step visit cap** (`BB_MAX_STEP_VISITS`, default 3): breaks runaway loops. A step returning the same non-happy verdict repeatedly is halted and escalated.
- **Per-step model enforcement**: ensures the intended (often cheaper) model actually runs.
- **Push-lands-or-retry**: failures retry from last good state.
- **Startup validation**: malformed `BB_ALLOWED_MODELS`, missing `GIT_REMOTE_URL` in git mode, etc. surfaced at boot.

## API endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Web UI |
| GET | `/api/config` | Workflows, policies, git mode, source branch, active model profile |
| POST | `/api/runs` | Start a run (body: `{workflow, policy, story_id, source_branch}`) |
| POST | `/api/runs/{id}/decision` | Approve / request changes at a gate |
| GET | `/api/runs/{id}` | Run state |
| GET | `/api/runs/{id}/file?path=` | Read an artifact (guarded: 2 MB cap, text-only, path-traversal guarded) |
| GET | `/api/poll?since=<cursor>` | Poll event stream (primary transport, cursor-based) |

## Running the project

```bash
# Build the agent image
docker build -t bheembhai/agent:latest agent/

# Set required env vars
export ANTHROPIC_API_KEY=sk-...
export BB_AGENT_IMAGE=bheembhai/agent:latest
export GIT_REMOTE_URL=https://github.com/you/your-repo.git
export GIT_SOURCE_BRANCH=main
export GH_TOKEN=ghp_...
export JIRA_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com
export JIRA_API_TOKEN=...
export BB_ALLOWED_MODELS="claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5"

# Run the server
uvicorn app:app --port 8000
```

## Testing


## Coding conventions

- **Python**: dataclass-driven domain model (`Run`, `Workflow`, `Policy`, `Handle`), class constants for enums (`ExecState`, `Result`), SQLite via raw SQL with `sqlite3.Row` factory, threaded execution with `EventBus` pub/sub
- **Bash**: `set -uo pipefail`, defensive JSON construction via `jq`, atomic writes (write `.tmp` then `mv`), heartbeat progress loop for liveness
- **Containers**: run as non-root `node` user (required for `--dangerously-skip-permissions`), host mounts set `0o777` for writability
- **Error handling**: auth failures classified by *which* credential (model 401 vs Jira vs git), diagnostics written before agent runs so they survive container deletion
- **Result extraction**: agent emits `BB_OUTCOME: <status>` and `BB_REVIEW: <path> | <note>` lines in its final reply — the runner script parses these, never names a result file in the agent prompt (that caused collision with skills' own `result.json`)
- **Credentials**: last-4-character fingerprint in diagnostics for verification without exposure, token embedded in git URL only during clone/push then scrubbed

## Important patterns

- **Failure hand-off**: When a step routes to another on a non-happy verdict, the next step's prompt includes the prior step's status, summary, and report files — making loops converge instead of spinning blindly.
- **Review file curation**: Skills declare `BB_REVIEW:` lines; the UI shows these curated files by default (with a toggle for all changed files). If none emitted, falls back to full git diff.
- **Context injection**: The backend builds per-step context with `{allowed_result_statuses, gate_follows, result_status_meanings, reviewer_feedback, upstream_handoff}` — the skill sees only its vocabulary, never routing targets.
- **Validation at run-submit time**: workflow/policy pairing validated before any container launches (HTTP 422 on mismatch), boot-time config checked with `BB_STRICT_STARTUP` option.
