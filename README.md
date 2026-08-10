# BheemBhai

**Agentic delivery, with a human in the loop where it counts.**

BheemBhai runs a library of product-development skills (story-design, test-creator,
implement, test-verify, code-review, pr-create) as a **governed, containerised, backend-
orchestrated pipeline**. Each step runs in its own ephemeral container; the backend owns
the flow, routes between steps, pauses for human approval where policy demands it, and
enforces model choice, cost, and loop limits.

The core idea: a fixed skill like `story-implement` bakes one team's process. BheemBhai turns
that fixed sequence into **per-project configuration** — a workflow composes governed skills,
a policy gates them, and feedback is wired where the project wants it.

---

## Table of contents

- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration reference (env vars)](#configuration-reference)
- [Workflows](#workflows)
- [Policies (human-in-the-loop)](#policies)
- [Result statuses](#result-statuses)
- [Git mode](#git-mode)
- [Model control & multi-vendor profiles](#model-control)
- [Guardrails](#guardrails)
- [Reviewer experience](#reviewer-experience)
- [Failure hand-off between steps](#failure-hand-off)
- [Validation](#validation)
- [Diagnostics & debugging](#diagnostics)
- [API endpoints](#api-endpoints)
- [Testing](#testing)
- [Deployment notes](#deployment-notes)

---

## Architecture

Three moving parts:

1. **Backend engine (`engine.py`)** — owns orchestration. Runs the workflow state machine,
   launches a container per step, reconciles results, applies policy gates, enforces model /
   cost / loop limits. The agent is a *dumb worker*; the backend decides routing.

2. **Agent container (`agent/`)** — a single image that runs one skill per invocation via
   `run_skill.sh`. It clones/checks-out code (git mode), runs Claude Code against the skill,
   commits + pushes, and reports a structured result. It never decides what runs next.

3. **Web UI + API (`app.py`, `static/index.html`)** — FastAPI backend with a polling-based
   single-file UI. Shows the live pipeline, artifacts, gates, and cost.

**Key design principles:**

- Backend owns orchestration; the skill's `next` hint is advisory, the workflow is authoritative.
- One ephemeral container per step/attempt — isolation first.
- Two signals on separate channels: the result payload (from the container) and the exit
  status (polled from outside). A reconciler joins them.
- Three overlays kept separate: **workflow** (what runs + routing), **policy** (which steps
  gate on a human), **notifications** (who's told). Boundary test: "removing a rule changes
  *which steps run* → workflow; only changes *where a human intervenes* → policy."

---

## Quick start

```bash
# 1. Build the agent image
docker build -t bheembhai/agent:latest agent/

# 2. Export configuration (see the reference below)
export ANTHROPIC_API_KEY=sk-...
export BB_AGENT_IMAGE=bheembhai/agent:latest
export GIT_REMOTE_URL=https://github.com/you/your-repo.git
export GIT_SOURCE_BRANCH=main
export GH_TOKEN=ghp_...
export JIRA_URL=https://you.atlassian.net
export JIRA_EMAIL=you@example.com          # Jira Cloud authenticates with email
export JIRA_API_TOKEN=...
export BB_ALLOWED_MODELS="claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5"

# 3. Run the server
uvicorn app:app --port 8000
# open http://localhost:8000
```

Install deps if needed: `pip install fastapi uvicorn pyyaml --break-system-packages`.

---

## Configuration reference

All configuration is via environment variables. Nothing is required to *start* except a way to
run work; git mode additionally needs a remote.

### Core

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Anthropic model credential (default profile). |
| `BB_AGENT_IMAGE` | `bheembhai/agent:latest` | The agent Docker image to launch. |
| `BB_WORKDIR` | `/tmp/bheembhai` | Host dir for state, results, clones, and the SQLite DB. |
| `BB_LOG_LEVEL` | `INFO` | Log verbosity. |
| `BB_STRICT_STARTUP` | `0` | `1` = refuse to boot on bad config instead of warning. |

### Git mode

| Variable | Default | Purpose |
|---|---|---|
| `BB_GIT_MODE` | `1` | `1` = clone/commit/push a real branch; `0` = copy-mode fallback. |
| `GIT_REMOTE_URL` | — | Repo to clone (required in git mode). |
| `GIT_SOURCE_BRANCH` | `main` | Branch the run branch is cut from (UI-overridable per run). |
| `GH_TOKEN` | — | Token used for clone/push and the GitHub MCP. |
| `BB_SEED_REPO` | — | Copy-mode only: local dir copied as the workspace (ignored in git mode). |

### Models

| Variable | Default | Purpose |
|---|---|---|
| `BB_ALLOWED_MODELS` | Anthropic set | Comma-separated allowlist for workflow model names. `*` disables. |
| `BB_MODEL_PROFILE` | `anthropic` | Selects `config/profiles/<name>.env` — remaps tiers per vendor. |
| `ANTHROPIC_AUTH_TOKEN` | — | Vendor credential when a profile sets `ANTHROPIC_BASE_URL`. |
| `ANTHROPIC_BASE_URL` | — | Vendor endpoint (usually set by the profile). |
| `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_EFFORT_LEVEL` | — | Passed through when set by a profile. |

### MCP (Jira / GitHub)

| Variable | Purpose |
|---|---|
| `JIRA_URL` | Jira base URL. |
| `JIRA_EMAIL` | Preferred for Jira Cloud auth (falls back to `JIRA_USERNAME`). |
| `JIRA_USERNAME` | Jira Server auth, or fallback if no email. |
| `JIRA_API_TOKEN` | Jira API token. |

### Guardrails & ops

| Variable | Default | Purpose |
|---|---|---|
| `BB_MAX_STEP_VISITS` | `3` | Halt-and-escalate after a step runs this many times (loop breaker). |
| `BB_KEEP_CONTAINERS` | `0` | `1` = keep stopped containers for debugging (`docker cp` to inspect). |
| `BB_MOCK` | `0` | `1` = fake runtime (no Docker), for engine testing. |
| `BB_DOCKER_ENDPOINT` | — | Remote Docker endpoint if not local. |
| `BB_SKILLS_DIR` | `agent/skills` | Where installed skills are discovered for validation. |

---

## Workflows

A workflow is a YAML state machine (`config/workflow-*.yaml`). Each step names a skill, a
model, a deadline, and its routing (`on:` map from result status → next step).

The shipped `story-delivery` workflow:

```
story-design → test-creator → implement → test-verify → code-review → pr-create → DONE
     │                                          │              │
     │escalation → tech-design                  │BLOCK         │changes_requested
     │changes_requested → (self)                ↓              ↓
                                            implement      implement
```

Per-step model assignment (tier names; a profile may remap them per vendor):

| Step | Model tier |
|---|---|
| story-design, tech-design | opus |
| implement, code-review | sonnet |
| test-creator, test-verify, pr-create | haiku |

Routing is **backend-authoritative**. `route_to` lets a step name its own target via the
result's `next` hint; otherwise the workflow's `on:` map decides. `DONE` ends the run.

> **Note:** the `on:` key must be quoted (`"on":`) in YAML — bare `on` parses as boolean `true`
> under YAML 1.1. The loader tolerates both.

---

## Policies

A policy (`config/policy-*.yaml`) decides **whether a human is consulted** on a step's outcome.
It never changes *where* control goes — only whether it pauses for approval first.

Each gate can declare `on_status: [...]` — the outcomes that require review (defaults to
`[completed]`). Three shipped policies:

- **strict** — humans approve the key gates.
- **governed** — humans approve *and* are pulled in on problems (e.g. test-verify `BLOCK`).
- **fast** — fully autonomous, no gates.

Approving a non-happy verdict (e.g. a `BLOCK`) means "yes, this verdict is valid, proceed to
address it" — control then follows the workflow's route for that status (e.g. back to
`implement`).

---

## Result statuses

The orchestrator owns a fixed status enum (structural meaning); the **skill decides which
applies**. Definitions describe *meaning*, never a routing destination.

| Status | Meaning |
|---|---|
| `completed` | The skill ran and its verdict is positive — proceed. |
| `BLOCK` | The skill ran fine and **found a real problem** (e.g. test-verify's honest-green check failed). A checker *succeeding* at finding a problem, not failing. |
| `changes_requested` | Work produced, but the skill (or a reviewer) wants revisions. |
| `escalation_required` | Something outside the skill's authority (e.g. an architecture change, or "can't verify here"). |
| `failed_execution` | Deterministic failure — the skill couldn't run (bad input, auth). |
| `failed_infra` | Transient infrastructure failure (e.g. a failed git push) — retried. |
| `failed_timeout` | Step exceeded its deadline — retried. |
| `failed_incomplete` | Container exited without publishing a result — retried. |

The domain statuses presuppose the skill ran fine; they differ in *verdict*. `failed_*` is the
separate "couldn't run" family.

---

## Git mode

Git mode (default) makes the work real: each run operates on a branch cut from the source
branch, and steps commit + push their output.

**Branch model:** every run auto-derives a unique branch `feat/<story>/<DDMMYYYYHHmm>`, cut
from the source branch (env `GIT_SOURCE_BRANCH`, overridable per run in the UI). The run *owns*
this branch exclusively; steps run sequentially, so there is no concurrent writer and no merge
handling is needed.

**Per step:** clone the run branch (creating it from the source branch on the first step),
run the skill, commit, and **push**. The token is embedded only for clone/push via an explicit
URL and scrubbed from `.git/config`, so it never persists on disk. Platform plumbing
(`.claude/`, `.mcp.json`) is excluded from commits.

**The invariant that makes half-way failures clean:** *a step counts only when its push lands.*
- A later step failing → earlier commits are safely on the branch (work preserved).
- A crash before push → work is lost; retry redoes it from the last pushed state.
- A push failure → `failed_infra`; retry from the last pushed state.

The branch always reflects exactly the completed steps — nothing partial.

**Copy mode** (`BB_GIT_MODE=0`) uses `BB_SEED_REPO` — a local dir copied as the workspace,
never pushed. Useful for demos without a remote. The UI hides the branch field in this mode.

---

## Model control

**The problem it solves:** a model named in a SKILL.md is *inert* — Claude Code does not switch
models because a file mentions one. Without enforcement, every step defaults to Opus regardless
of intent (this cost real money in testing). BheemBhai enforces the model per step.

**Per-step enforcement:** the workflow's `model:` field is passed as `--model` to Claude Code.
The actual model(s) used are recorded from the run's `modelUsage` into the result and
diagnostics, so "did the right model run?" is always answerable.

**Multi-vendor profiles (Option A — vendor-neutral workflows):** the workflow names *tiers*
(`claude-opus-4-8` / `claude-sonnet-4-6` / `claude-haiku-4-5`); a **profile file** decides which
real model each tier resolves to, plus the vendor's endpoint/knobs. Switching vendor is
"point `BB_MODEL_PROFILE` at a different file" — no code or workflow change.

Shipped profiles in `config/profiles/`:

- `anthropic.env` (default) — tiers map to real Anthropic models.
- `deepseek.env` — tiers → `deepseek-v4-pro` / `deepseek-v4-flash`, endpoint + `EFFORT_LEVEL=max`.
- `kimi.env` — tiers → `kimi-k3`, Moonshot endpoint.

```bash
# Run everything on DeepSeek — same workflow, no changes:
export BB_MODEL_PROFILE=deepseek
export ANTHROPIC_AUTH_TOKEN=<deepseek token>   # secret — NOT stored in the profile file
export BB_ALLOWED_MODELS="claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5"  # tier names, unchanged
```

> Secrets never live in profile files. The profile maps tiers + endpoint; the auth token comes
> from the environment (or AWS Secrets Manager in production).

---

## Guardrails

- **Per-step visit cap** (`BB_MAX_STEP_VISITS`, default 3) — breaks runaway loops. A workflow
  can legitimately loop (test-verify `BLOCK` → implement → test-verify), but a step that keeps
  returning the same non-happy verdict is halted and escalated to a human.
- **Per-step model enforcement** — bounds per-step cost by ensuring the intended (often cheaper)
  model actually runs.
- **Push-lands-or-retry invariant** — a step's work only counts once pushed; failures retry from
  the last good state rather than corrupting the branch.

> A **per-run budget cap** (hard dollar ceiling) is designed but not yet built — recommended
> before this carries real customer work on an always-on host.

---

## Reviewer experience

- **Curated review files.** A skill can emit `BB_REVIEW: <path> | <note>` lines to declare which
  files a human should actually review. The UI shows these by default ("the author asks you to
  review these") instead of every git-touched file — with a "show all N changed files" toggle so
  nothing is ever hidden. If a skill emits none, the UI falls back to the full changed list.
- **Clickable artifacts.** Every listed file opens in a viewer (2 MB cap, text-only, path-
  traversal guarded).
- **Gate cards.** At a paused gate, the reviewer sees the outcome, the files, and can approve or
  send back with a comment that flows into the next attempt's context.

---

## Failure hand-off

When a step routes onward on a **non-happy verdict**, the next step is told why and pointed at
the prior step's committed report. Example: on `test-verify → BLOCK → implement`, implement's
prompt gets:

> "You are being run because the 'test-verify' step returned 'BLOCK', which routes here to be
> addressed. Read its report first: docs/verification.md. Address every point it raises."

This is **step-agnostic** — any loop-back (code-review → implement, escalation → tech-design)
hands off the same way, carrying `{from_step, status, summary, report_files}` from the result.
It makes loops *converge* (fix the named problem) rather than spin blindly. No stale hand-off
leaks into later steps.

---

## Validation

Caught at run-submit time (HTTP 422, before any container launches):

- **`validate_workflow`** — rejects: a routing target that isn't a defined step; an unknown
  model (checked against `BB_ALLOWED_MODELS`); a `start` step that isn't defined; a step with no
  skill or an uninstalled skill.
- **`validate_pairing`** — rejects a policy that gates on a status the workflow can't route from
  that step (a human would approve into a dead end), or gates an unknown step.

Caught at **boot** (`check_startup_config`, logged; `BB_STRICT_STARTUP=1` refuses to start):

- A malformed `BB_ALLOWED_MODELS` (e.g. a missing comma gluing two model ids).
- Git mode on with no `GIT_REMOTE_URL`.

---

## Diagnostics

Each step writes `/out/diagnostics.txt` **before** the agent runs (survives container deletion):
uid, permission flag, credential presence with last-4 fingerprints (e.g. `ends ...UgAA`), the
model flag and resolved model, the active profile and vendor endpoint, `uvx` availability, and
MCP config/readiness.

**Auth-failure classification** — the runner distinguishes *which* credential failed:
- Model 401 → "ANTHROPIC_API_KEY rejected by the model API — NOT a Jira or git problem."
- Jira/Atlassian MCP auth failure → named separately.
- Git push/clone failure → `failed_infra` with the branch named.

Set `BB_KEEP_CONTAINERS=1` to keep stopped containers, then `docker cp <id>:/out/agent.log .`
(the container has exited — use `docker cp`, not `exec`). Per-step logs also live on the host
under `$BB_WORKDIR/results/<run_id>/<step>/<attempt>/`.

---

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The web UI. |
| GET | `/api/config` | Workflows, policies, git mode, source branch, active model profile. |
| POST | `/api/runs` | Start a run (`workflow`, `policy`, `story_id`, `source_branch`). |
| POST | `/api/runs/{id}/decision` | Approve / request changes at a gate. |
| GET | `/api/runs/{id}` | Run state. |
| GET | `/api/runs/{id}/file?path=` | Read an artifact (guarded, 2 MB, text-only). |
| GET | `/api/poll?since=<cursor>` | Poll the event stream (primary transport). |

---

## Testing

```bash
python3 test_engine.py      # 21 tests, fake runtime, no Docker required
```

Coverage includes: happy path through gates, retries and escalation, `BLOCK` routing and
loop-back, per-status policy gating, workflow/policy pairing validation, the visit cap,
per-step model reaching launch, multi-vendor tier resolution, review-file hand-off, and the
upstream failure hand-off (any source→target pair, no stale leak).

---

## Deployment notes

- **Target: internal tool (single EC2 + local Docker).** The app talks to the local Docker
  socket and spawns agent containers on the same box — the code already works this way; the
  `DockerRuntime → FargateRuntime` swap is the graduation to multi-tenant scale (the runtime
  seam is designed for it).
- **Secrets** (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `GH_TOKEN`, `JIRA_API_TOKEN`) belong
  in **AWS Secrets Manager**, injected as env at launch — never in profile files or the image.
- **Profile files** are non-secret config; ship them in the repo / image.
- **Auth & state** for the internal-tool build: Cognito (via ALB) for login, RDS Postgres for
  state, per-user/per-customer run history. Build order: Postgres → identity on runs → Cognito
  → history views. (Designed; not yet built.)
- **Before customer work:** land the per-run budget cap — an always-on box that named users
  trigger against real repos needs a hard cost ceiling.

---

*BheemBhai — the platform is a configurable workflow engine; `story-implement` is a hardcoded
version of the same thing.*
