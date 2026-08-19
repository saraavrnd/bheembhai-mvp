# ADR-014: Results over Object Storage — zero-mount step containers

**Status:** Accepted · **Date:** 2026-08-19 · **Deciders:** Saraav
**Amends:** ADR-011 (protocol — presigned PUT), ADR-013 (env bundle §5, runtime §4 — no mounts),
`docs/architecture.md` (Step execution, Context passing), `CLAUDE.md` (coding conventions)

## Context

Phase 1 (ADR-013 §3) moved skill delivery to Object Storage via presigned GETs, but the step
container still carries two bind mounts:

- `/out` — the result payload, `progress.json`, `agent.log`, and `diagnostics.txt`, read by
  the engine from host paths after the container exits;
- `/workspace` — the disposable git clone (push-lands-or-retry means the clone tree is never
  authoritative).

The mounts exist only because **the engine reads files post-exit** from host paths and the
**platform viewer** falls back to reading the engine's clone tree (`BB_WORKDIR`). Host path
parity (the same absolute path on host, engine container, and daemon) is fragile — a
container-only path makes the daemon silently create a root-owned tree the agent's non-root
`node` user cannot write. Host mounts also block the deferred `FargateRuntime`: a Fargate task
has no host filesystem to mount.

Every remaining runtime channel is a blob with a known size and content type. The storage
boundary already exists (ADR-011) and already flows secrets to the agent safely (presigned
URLs — ADR-013 §3). The last host-dependent bits can flow through it the same way.

## Decision

**All four runtime channels flow through Object Storage under deterministic, timestamp-free
keys. Step containers get zero mounts.** The agent uploads via per-launch presigned PUT URLs;
the engine reads back from the store and uploads one channel itself (container.log, which only
the engine can see — it comes from the docker API).

### Contract

| Env var (per launch) | S3 key | Uploader | Critical? |
|---|---|---|---|
| `BB_RESULT_PUT_URL` | `results/<run>/<slug step>/<attempt>/bb_step_result.json` | agent, EXIT trap | **YES — PUT failure → verdict rewritten to `failed_infra`, exit 4 (retried)** |
| `BB_PROGRESS_PUT_URL` | `results/<run>/<slug step>/<attempt>/progress.json` | agent, heartbeat every 5s | no (best-effort) |
| `BB_LOG_PUT_URL` | `logs/<run>/<slug step>/<attempt>/agent.log` | agent, heartbeat + exit | no (≤5s staleness OK for cost scrape) |
| `BB_DIAG_PUT_URL` | `logs/<run>/<slug step>/<attempt>/diagnostics.txt` | agent, exit | no |
| (none — engine-side) | `logs/<run>/<slug step>/<attempt>/container.log` | engine via docker API → `store.put` | no |

- **Keys** are deterministic and timestamp-free (crash re-attach writes to the same key) and
  idempotent-overwrite. Two namespaces: `results/` = live step channels, `logs/` = attempt
  logs. Key construction is centralized in `shared/bheembhai/log_keys.py`.
- **Content types**: `application/json` for result/progress; `text/plain` for logs (agent
  curl sends an explicit header — `--data-binary` alone would send form-urlencoded).
- **Missing URL in agent env → that upload is skipped**, no retry, never a failure; one
  script-start warning when all four are absent (host test mode).
- **Presign failure in the engine** → the existing `failed_infra` retry path (mirrors the
  `BB_SKILL_URL` block). `store is None` or `presigned_put_url` returns None (LocalStorage —
  `file://` URLs cannot be PUT by curl) → warn + omit the URL.
- **Expiry**: `expires_in = max(3600, deadline + 600)` — the critical result PUT happens at
  exit, possibly after a long step.
- **Result upload happens after the git push by construction** — a result-upload failure
  retries the step and the retry re-pushes idempotently from landed state (exactly today's
  push-then-crash semantics; the push-lands-or-retry invariant is untouched).
- **Cadences**: the agent re-uploads progress + agent.log every 5s; the reconciler keeps the
  fast 0.4s status poll and moves Object Storage reads to a ~2s slow cadence **plus** an
  immediate read while exited within the grace window (preserves read-on-exit; S3 read-after
  PUT is strongly consistent and the agent's trap PUT completes before docker reports exited).
- **Presigned PUT URLs are bearer credentials**: never logged in full — the agent prints
  nothing; engine log lines reference keys, not URLs.

### Runtime + viewer

- `DockerRuntime` drops its `workdir` and launches containers with **no `volumes`**,
  `working_dir="/workspace"` (image-owned, created in the agent Dockerfile). The Runtime
  `Handle` is key-less (`container_id, started_at, run_id, step_id, attempt_no`); channels are
  derived keys.
- Log registration (`upload_step_logs`) becomes **registration-only**: head-check each
  `logs/…` key in the store, select-or-insert the `RunLog` row with the store's size — no
  data movement.
- The platform viewer's fallback chain becomes **git-at-SHA → demo stubs → placeholder**: the
  `BB_WORKDIR` clone-tree stage is deleted. Accepted regression: never-committed generated
  artifacts (e.g. `changes.diff` pills) become placeholder-only.

## Alternatives considered

- **Conductor-style engine polling of a shared stream (rejected/deferred):** richer but
  unneeded at MVP — a per-step presigned PUT per channel is the smallest change that removes
  the mounts. (Deferred with conductors: project MCP.)
- **Artifacts read from the git tree at the recorded SHA only (rejected as exclusive
  source):** correct for committed work but loses generated artifacts; stubs keep the demo
  experience for those.
- **Keep the mounts behind a Fargate flag (rejected):** two code paths for every channel,
  and the host-dependent path would rot untested.

## Implementation notes (2026-08-19)

Implemented across the shared key module (`log_keys.py`), the engine (`state_machine.py`
launch contract `_launch_upload_contract`, zero-mount `runtime.py`, registration-only
`log_upload.py`), the agent (`run_skill.sh` heartbeat + EXIT trap with `${BB_*_PUT_URL:-}`
expansions — unbound-var in a trap aborts it silently under `set -u`), and the platform
(`github_content.py` + `runs.py` viewer chain). `EngineConfig.workdir` / `BB_WORKDIR` are
deleted; compose defaults `LOCAL_STORAGE_PATH=/tmp/bheembhai-artifacts` and binds only the
source tree and `docker.sock`.

**Upgrade order (half-merge behavior is asymmetric):**

- new engine + old agent image → no mounts, old script's `/out` stays container-local → every
  step `failed_incomplete` → retries → run fails after max_attempts. Fails SAFELY (pushes
  still land; no wrong commits) but is unrecoverable without an image rebuild.
- new agent script + old engine → sees no PUT URLs → warns and works exactly as before
  (fully compatible).

So: merge → **rebuild + push the agent image BEFORE deploying the new engine** → deploy
engine → deploy platform. In-flight runs across a restart re-attach, classify
`failed_incomplete`, and retry with a fresh new-image container — acceptable; no schema
migration (`logs/` keys unchanged; `results/` is a new prefix).

## Consequences

- **Easier:** Zero host mounts — no path parity, no 0o777 trees, no root-owned daemon
  artifacts; `FargateRuntime` is unblocked (env-only launch contract).
- **Easier:** One mechanism (presigned URLs) for every container channel — the AWS creds
  rule is uniform: agents never receive credentials, only scoped URLs.
- **Harder:** Every step costs a handful of PUTs/GETs; the result channel is now network —
  mitigated by retries, long expiries, and the critical-only degradation rule.
- **Harder:** Progress/agent.log reads are up to ~5s stale (heartbeat cadence); container.log
  remains exact (engine-side).
- **Harder:** `local` storage backend + real containers now fails every step
  (`failed_incomplete` — no PUT URLs); LocalStorage is valid for host-run tests only.
- **Harder:** Generated-artifact viewing regresses to placeholders when the artifact was
  never committed.
- **Doc updates required:** `architecture.md` (step execution, context channels, component
  rows), `CLAUDE.md` (agent container, coding conventions, API table), `.env.example`,
  `docker-compose.yml` comments.
