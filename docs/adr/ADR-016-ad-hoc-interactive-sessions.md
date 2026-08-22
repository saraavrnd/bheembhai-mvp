# ADR-016: Ad-hoc interactive sessions (multi-turn live container)

**Status:** Accepted · **Date:** 2026-08-21 · **Deciders:** Saraav
**Amends:** ADR-003 (crash recovery — session re-delivery paths), ADR-013 (run init §2 —
session id mint, branch verify-adopt), ADR-014 (object-storage channels — inbox/outbox/
transcript), `CLAUDE.md` (run kinds, session lifecycle, API surface)

## Context

The platform runs exactly one thing: a governed pipeline. A run picks a workflow, the engine
walks a fixed sequence of skills, and every container executes one skill behind a hardcoded
prompt — `SKILL` is mandatory, and no field carries a free-form query. There is no way to say
"check out this branch and just do this thing for me".

We want an ad-hoc mode: name an existing branch, type a query, watch Claude Code work, and
keep talking to it — the feel of a terminal session, inside the governed platform so cost,
audit, cancel, and crash recovery still apply.

Everything this needs already exists in pattern form: the work_queue dispatch + per-run lock
(ADR-003), the two-signal reconciler and re-attach via `runtime.make_handle()` (ADR-014), the
S3 skill-bundle delivery with sha256 pinning, the presigned-URL launch contract, and the
bookkeeper split (ADR-013 §1).

## Decision

**An `adhoc` run kind reuses the run/step/transition machinery with a multi-turn session:
one long-lived container serves turns fed through object-storage inbox/outbox channels,
pausing the run at `awaiting_input` between turns. An idle reaper ends the session
gracefully; the next turn cold-starts a fresh container and resumes the conversation from
the session transcript.**

### Model

- Run columns: `run_kind` (`workflow` | `adhoc`), `user_query` (nullable — the opening prompt,
  persisted so it survives restarts), `claude_session_id` (uuid4 minted at `_init_run`,
  idempotent), `session_phase` (`pending` → `active` → `reaping` → `ended`),
  `session_last_activity_at` (reaper clock).
- Step columns, re-read for sessions: `attempt_no` numbers **container incarnations** (a reap
  or crash cold-starts a new one — it is not a retry); `turn_no` numbers **turns** (the global
  seq the channels match on). `exec_state` stays `running` for the whole session — the live
  container *is* the session — so `pending` means "never launched" (the cold-start
  discriminator).
- Sessions reuse the workflow machinery untouched: a real `adhoc` skill row (S3 bundle
  pinning, sha256 verify, self-healing publish all work as-is), a 1-step workflow with
  `"on": {completed: DONE}`, and a `gates: {}` policy (required by schema). No changes to the
  step loop.
- **New pause kind `awaiting_input`** — a Transition `to_state`, never an approval gate. One
  dispatch advances a run exactly one pause: a turn *is* a pause. `work_queue` items carry
  `{action: turn, query}` or `{action: end}`.

### Channels (ADR-014 extension)

- `turns/<run>/<slug step>/<attempt>/inbox.json` — engine→container. **One stable key per
  attempt, overwritten per turn**, carrying a monotonic `seq`. One presigned GET lasts the
  container's whole life; the agent detects a new turn by `seq` change (how `progress.json`
  already works in reverse).
- `turns/<run>/<slug step>/<attempt>/outbox.json` — container→engine, seq-matched reply
  `{seq, response, commit, files, cost_usd, cost_reported}`.
- The **end sentinel** `{seq: turn_no+1, kind: "end"}`: the seq is *derived* from `turn_no`
  but **never persisted** — `turn_no` numbers turns, and a sentinel that never completes as a
  turn must not consume a turn number (the next real turn takes that seq: it either ends the
  session here, or is superseded by that turn, never both).
- Both keys join `_launch_upload_contract` and `_clear_attempt_channels` — stale-channel
  replay is a bug this repo has already been bitten by (run 07c4b440); a launch clears its
  attempt's channels first.

### Turn flow (engine)

`_session_turn`: commit the next seq (`turn_no+1`, persisted) → `_live_session_handle`
(re-attach via `make_handle` when the fargate arn is set: running → adopt; exited/gone →
cleanup + cold-start with `attempt_no+1`; **the first launch keeps the fresh `attempt_no` 1**
— `exec_state` pending, not running, means never launched) → write the inbox → `reconcile_turn`
(polls status every 0.4 s, outbox every 5th tick; **returns without killing the container on
the happy path** — the container *is* the session. Terminal conditions: cancel event →
cancelled; container gone → `failed_infra`; exited without a reply after a 3 s grace →
`failed_incomplete`; deadline → `failed_timeout` + cleanup) → `_finish_turn` (run paused,
phase active, Transition `{kind: "turn", seq, query, response, commit, files, cost}`).

**Each completed turn is a Transition row** with query + response + commit + cost in the JSONB
payload — the durable, auditable turn history, independent of object storage. Cost accrues
per turn on run + step.

### Session end + idle reap

- **Explicit End** (`_end_session`): sentinel → `_wait_container_exit` — **never
  `docker rm -f` here**: the exit is the moment the final commit lands, and R1 (unpushed work
  dies with the container) rules out a kill. exit 0 / no live container (a prior reap already
  committed) → run `completed`, phase `ended`. Anything else keeps the session open — paused,
  so the user can end again or send another turn (which cold-starts).
- **Idle reaper** (`_reap_idle_adhoc_sessions`, worker sweep each loop iteration — one cheap
  query, committed independently): paused ad-hoc runs idle past `BB_ADHOC_IDLE_SECONDS`
  (default 600) with no busy work item. The flip to `reaping` is atomic (rowcount guard —
  double-fire safe); a per-run task writes the sentinel and waits with an `alive_check` that
  aborts as *superseded* if a turn dispatch flips the phase back to `active`. Hung past
  `BB_ADHOC_END_GRACE_SECONDS` (default 60) → hard-kill (loses only the transcript — turns
  commit per turn). Finalize is conditional (`UPDATE … WHERE session_phase='reaping'`): the
  run **stays paused** — the session ends, the run keeps accepting turns.

### Resume (reap becomes resumable)

The engine mints the session UUID up front, so the transcript filename is deterministic —
no scraping it from agent output. The agent uploads `~/.claude/projects/<munged-cwd>/<id>.jsonl`
on graceful exit to `transcripts/<run>/<session_id>.jsonl`
(`BB_TRANSCRIPT_PUT_URL` always; a missing URL just skips the upload, ADR-014). A cold-start
incarnation restores it to the same path and runs `--resume <id>
--exclude-dynamic-system-prompt-sections` (`BB_SESSION_RESUME=1`, `BB_TRANSCRIPT_GET_URL`
provided on resume only — feature-detected like `--mcp-config`). **`CLAUDE_VERSION` is pinned
in the agent image** (2.1.218 at time of writing): the transcript JSONL is version-internal,
so `latest` silently drifting mid-session would break resume.

### Governance + branch

Wide open by design: existing project run access governs (no new authz surface), **any
existing branch** — the engine verifies the named ref via GitHub REST (404 →
`InitFailure("failed_execution")`) and works directly on it (`run.run_branch` preset skips
derive/create entirely). No gates: `submit_decision` 409s on an ad-hoc run ("no approval
gates — use /turn or /end"). Approved non-happy verdicts in the ad-hoc `on:` map are outside
the vocabulary; the workflow routes only `completed: DONE`.

### API + UI

- `POST /api/runs/{id}/turn` `{query}` — validates the session is awaiting input
  (`_session_dispatch_guard`: 409 unless paused + ad-hoc + phase active/pending), enqueues a
  `continue` item. The platform never mutates run state (bookkeeper, ADR-013 §1).
- `POST /api/runs/{id}/end` — same guard, enqueues `{action: end}`.
- `POST /api/runs` gains `query` + `branch` (an existing ref) — `story_id` optional for
  ad-hoc. One run = one session.
- UI: a chat template (transcript renderer + live log view — RunLog rows are now registered at
  launch, so `agent.log` tails while the step is in flight) and a workflow-catalog card so
  sessions appear alongside pipelines.

## Alternatives considered

- **Pipe the query over docker attach/exec or a stdio socket (rejected):** reintroduces
  host-attached transport that ADR-014 removed and would not port to Fargate; the presigned
  inbox/outbox channels work across runtimes with zero new infrastructure.
- **One container per turn (rejected):** cold-start latency on every message kills the
  terminal feel and the working context; the hybrid keeps a live container while the reaper
  bounds its lifetime.
- **`rm -f` / SIGKILL at reap (rejected):** unpushed work dies with the container (R1) —
  the sentinel + wait is load-bearing, the hard-kill only a grace-window fallback.
- **Per-turn approval gates (rejected):** governance is deliberately wide open (project run
  access); the idle timeout is the only bound on the session's lifetime.
- **Engine-derived branch for ad-hoc (rejected):** the user wants work on *their* existing
  branch — verify-adopt, never create.
- **WebSocket/SSE into the container (rejected):** a second transport for one feature; the
  engine polling object storage (0.4 s status / 2 s outbox) matches turn granularity.

## Consequences

- **Easier:** cost, audit, cancel, and crash recovery are inherited from the run machinery;
  every turn is a durable Transition row; reaped sessions resume mid-conversation.
- **Easier:** no new authz surface — project run access governs sessions exactly as it
  governs pipeline runs.
- **Harder:** a live container holds credentials resident under
  `--dangerously-skip-permissions` for the session (R3); the reaper is load-bearing, not a
  nicety (R2 — `mem_limit` caps idle concurrency).
- **Harder:** `awaiting_input` is a genuinely new pause kind — every `state == "paused"`
  branch must be audited (R5, done: `submit_decision`, recovery, cancel all handle ad-hoc
  explicitly).
- **Harder:** resume depends on the pinned CLI version and a successful transcript upload —
  a hard-killed container loses the transcript (turns survive; conversation continuity
  doesn't).
- **Doc updates required:** `CLAUDE.md` (run kinds, session lifecycle, API table), this ADR.
