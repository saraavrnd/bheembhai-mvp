# ADR-003: Engine Service as the background worker (no SQS/Redis/Step Functions)

**Status:** Accepted · **Date:** 2026-08-10 · **Amended:** 2026-08-11 · **Deciders:** Saraav

## Context

Run execution is long-lived: a single run spans 7+ steps, each step takes 30 seconds to 30
minutes (Claude Code in a Fargate container). The architecture needs to start a run, wait for
Fargate tasks to complete (potentially across many minutes), and route to the next step — all
without blocking a browser request.

Common patterns for this in AWS:
- **SQS + Lambda/ECS:** Platform API enqueues a "start step" message; a worker dequeues and
  launches Fargate; on completion, it enqueues the next step. Decoupled, but adds queue
  management (DLQ, visibility timeouts, message duplication).
- **Step Functions:** AWS native state machine, handles retries, timeouts, and the
  wait-for-task-completion pattern. But replaces the engine's existing state machine (873 lines
  of tested code) and couples workflow logic to AWS infra.
- **Redis/ARQ or Celery:** A task queue the engine polls. Adds Redis as a dependency and
  requires the engine to be both a queue consumer AND a state machine — doubling the async
  mechanisms.

The question: which background-job mechanism is the right fit for a two-service architecture
where the engine already owns the state machine logic?

## Decision

**The Engine Service itself is the background worker.** It runs as a long-lived FastAPI process
(managed by systemd or ECS). Internally, it uses asyncio tasks to manage the state-machine
loop: launch a Fargate task via boto3 → poll `describe_tasks()` until STOPPED → read the result
from Object Storage → reconcile → determine the next step → repeat or pause for approval.

**The Platform API and Engine communicate through a Postgres-backed work queue, not HTTP calls.**
The Platform API writes work items to a `work_queue` table. Engine processes pull work via
`SELECT ... FOR UPDATE SKIP LOCKED` — a FIFO queue with zero additional infrastructure.
This replaces the initial design where Platform API called Engine's `/engine/runs` endpoint
directly, which didn't scale to multiple Engine processes.

**Work items are claimed, not deleted.** The Engine atomically sets `state = 'claimed'` on a
work item (with `claimed_by` and `heartbeat_at`) rather than deleting it. This preserves the
item for crash recovery: if an Engine dies, the recovery pass detects a stale heartbeat and
re-enqueues orphaned work.

No SQS. No Redis. No Step Functions.

### Work queue table

```sql
CREATE TABLE work_queue (
    id            BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES runs(id),
    action        TEXT NOT NULL CHECK (action IN ('start', 'continue')),
    payload       JSONB NOT NULL DEFAULT '{}',
    state         TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'claimed', 'done')),
    claimed_by    TEXT,           -- engine instance ID (hostname or UUID)
    claimed_at    TIMESTAMPTZ,
    heartbeat_at  TIMESTAMPTZ,    -- last liveness ping (every 30s)
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Pull model (Platform API writes, Engine pulls)

```
Platform API                    Postgres                    Engine-1  Engine-2
    │                              │                           │         │
    │  INSERT INTO work_queue      │                           │         │
    │  (run_id, action: 'start')   │                           │         │
    │─────────────────────────────►│                           │         │
    │                              │                           │         │
    │  201 + run_id                │                           │         │
    │◄─────────────────────────────│                           │         │
    │                              │                           │         │
    │                              │  UPDATE ... SKIP LOCKED   │         │
    │                              │  state='claimed'          │         │
    │                              │◄──────────────────────────│         │
    │                              │                           │         │
    │                              │                           │  _run_loop(run_id)
    │                              │                           │  launches Fargate...
    │                              │                           │  heartbeats every 30s
```

### Heartbeat + crash recovery

Each Engine process has a unique `ENGINE_ID` (hostname or UUID). While a run loop is active,
a background asyncio task updates `work_queue.heartbeat_at` every 30 seconds.

**Recovery on startup** (`recover_on_startup()`, called before `worker_loop()`):

1. **Reclaim orphaned queue items:** query `work_queue` for rows where `state = 'claimed'`
   AND `heartbeat_at < NOW() - 60s` (stale heartbeat = dead Engine). Re-set them to
   `state = 'pending'` so the worker loop picks them up again.

2. **Reconcile in-flight runs:** query `runs` for rows where `state IN ('running',
   'retrying')`. For each:
   - If the Fargate task completed while the Engine was dead → read result from Object
     Storage, classify outcome, persist step, route to next step or DONE.
   - If the Fargate task is still running → re-enqueue a work item so the live Engine
     resumes polling.
   - If the Fargate task ARN is missing (Engine died before launch) → re-enqueue a work
     item so the step is re-launched.

The two sources of truth (`work_queue` for dispatch, `runs` for execution state) converge
on recovery: the queue handles "who picks up what," the runs table handles "what actually
happened."

### Why DELETE-on-pickup was rejected

An earlier amendment proposed `DELETE FROM work_queue ... SKIP LOCKED` to claim work. This
orphans in-flight runs when the Engine crashes: the work item is gone from the queue, but
the run is mid-execution with no owner. On restart, the worker loop sees an empty queue and
the run stays stuck in `running` forever. The claim+heartbeat pattern fixes this — the work
item persists in `claimed` state until the run reaches a terminal state, and stale claims
are detectable via the heartbeat timestamp.

## Alternatives considered

- **SQS + Engine worker (rejected):** SQS would decouple the Platform API from the Engine,
  which is valuable if they scale independently. But it adds queue management (DLQ,
  visibility timeouts, idempotency for the approval-continue path), and the Engine still needs
  the same state-machine loop internally. The decoupling benefit doesn't justify the added
  infrastructure for MVP scale.
- **AWS Step Functions (rejected):** Native retry/timeout/wait-for-task patterns. But Step
  Functions replaces the engine's core IP (the state machine, result reconciliation, policy
  gate evaluation) with AWS-proprietary ASL. The engine's logic is tested and carries forward;
  discarding it for infra coupling is the wrong trade.
- **ARQ/Celery + Redis (rejected):** Adds Redis as a dependency for queueing. The Engine would
  need to be both a queue consumer (polling Redis) AND manage Fargate task lifecycles. Two
  async mechanisms when one (asyncio) suffices.
- **DELETE-on-pickup work queue (rejected):** Simpler than claim+heartbeat but cannot survive
  Engine crashes — work items are ephemeral and run state is durable, creating a split-brain
  between the two sources of truth. The heartbeat mechanism closes this gap with ~10 lines of
  code and a 30-second UPDATE.

## Consequences

- **Easier:** No additional infrastructure (SQS queues, Redis clusters). One less thing to
  provision, monitor, and pay for.
- **Easier:** The Engine's asyncio state-machine loop is the same pattern it already uses
  today (poll Docker status in a loop) — just swapped to boto3 Fargate APIs.
- **Easier:** Recovery from Postgres. On restart, the Engine's `recover_on_startup()` pass
  queries for stale claims and in-flight runs and resolves them — no messages stuck in a
  queue to reconcile.
- **Easier:** Multiple Engine processes scale transparently. `SKIP LOCKED` ensures no two
  Engines grab the same work item. Add Engine-3, Engine-4 — they all pull from the same
  `work_queue` table.
- **Harder:** The `work_queue` table accumulates rows. Mitigated: `state = 'done'` rows are
  pruned by a periodic cleanup task (keep last 7 days). The table is append-mostly and small
  (one row per run, not per step — a run spawns its own asyncio task that loops internally).
- **Harder:** Heartbeat tuning. 30s is conservative (fits within Fargate's 5-minute+ task
  durations). Too short → false-positive re-enqueue on temporary DB latency. Too long → slow
  recovery. The value is configurable via `BB_ENGINE_HEARTBEAT_SECONDS`.
- **Harder:** Scaling the Engine to multiple processes requires coordination for Fargate task
  lifecycle (two Engines must not re-launch the same step). Mitigated by the `work_queue`
  state machine: only one Engine claims a given work item, and `_run_loop` checks `steps`
  state before launching (idempotency guard).
