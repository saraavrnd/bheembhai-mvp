"""Runtime protocol + DockerRuntime — the pluggable step-execution backend (ADR-013 §4).

The Runtime protocol is the seam between the engine state machine and whatever actually
runs step containers. `DockerRuntime` serves local dev (docker-compose); a FargateRuntime
behind the same protocol is the production variant (deferred).

Design notes carried forward from the R&D engine (engine.py):
  - Two independent signals: the result payload (agent-uploaded to object storage via a
    presigned PUT) and the exit status (polled from the runtime). A crashed container
    cannot report its own death.
  - The reconciler joins those signals against a deadline to classify each attempt.
  - Zero host mounts (ADR-014): /out and /workspace are image-owned container-local
    dirs; the engine reads every channel back from object storage under deterministic
    keys derived from (run_id, step_id, attempt_no).
"""

import asyncio
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from typing import Protocol

from bheembhai.log_keys import (
    RESULT_FILENAME,
    log_key,
    progress_key,
    result_key,
    turn_outbox_key,
)

log = logging.getLogger(__name__)

GRACE_SECONDS = 3.0
POLL_INTERVAL = 0.4
# Cadence for the object-storage channel reads (result payload + progress),
# in poll ticks: ~2s at POLL_INTERVAL=0.4. The docker status poll stays fast —
# it is the channel that detects death; S3 reads only need liveness granularity.
SLOW_POLL_TICKS = 5

# Sentinel returned by reconcile() when the run's cancel event fires mid-step.
# Deliberately NOT a Result/ExecState value: it means "the orchestrator aborted
# this attempt", not "the step produced an outcome" — the state machine handles
# it before any vocabulary check.
CANCELLED = "__cancelled__"


class Result:
    COMPLETED = "completed"
    BLOCK = "BLOCK"
    CHANGES_REQUESTED = "changes_requested"
    ESCALATION_REQUIRED = "escalation_required"
    FAILED_EXECUTION = "failed_execution"      # deterministic
    FAILED_INFRA = "failed_infra"              # transient
    FAILED_TIMEOUT = "failed_timeout"          # transient
    FAILED_INCOMPLETE = "failed_incomplete"    # transient


TRANSIENT = {Result.FAILED_INFRA, Result.FAILED_TIMEOUT, Result.FAILED_INCOMPLETE}


class ExecState:
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_RESULT = "awaiting_result"
    AWAITING_APPROVAL = "awaiting_approval"
    # Ad-hoc sessions (ADR-016 §2): the run pauses between turns — a transition
    # state (like AWAITING_APPROVAL), never a step-row exec_state.
    AWAITING_INPUT = "awaiting_input"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Handle:
    """A launched step container — everything needed to poll and clean it up.

    Channels are object-store keys derived from (run_id, step_id, attempt_no)
    — never disk paths: /out and /workspace are image-owned container-local
    dirs, and the engine reads results/progress/logs back from storage
    (ADR-014). A rebuilt Handle (crash re-attach) derives the same keys.
    """
    container_id: str
    started_at: float
    run_id: str
    step_id: str
    attempt_no: int


class Runtime(Protocol):
    """Async step-execution backend: launch a container, poll it, collect its artifacts."""

    async def launch(
        self,
        run_id: str,
        step_id: str,
        attempt_no: int,
        env: dict[str, str],
        *,
        context: dict | None = None,
    ) -> Handle:
        """Launch one step container. `env` is the fully composed bundle (ADR-013 §5)
        including the presigned PUT URLs for this attempt's channels (ADR-014);
        `context` (the per-run context dict) rides in as BB_CONTEXT — the runner
        writes CONTEXT_FILE inside the container (no /ctx mount)."""
        ...

    async def make_handle(
        self,
        run_id: str,
        step_id: str,
        attempt_no: int,
        container_id: str,
        started_at: float,
    ) -> Handle:
        """Rebuild a Handle from persisted state (crash re-attach, ADR-003).

        `container_id` comes from `steps.fargate_task_arn` (reused as the generic
        runtime handle) and `started_at` is the ORIGINAL launch time — deadline
        math must measure from the first launch, not from the recovery."""
        ...

    async def status(self, h: Handle) -> dict:
        """{"state": "running"|"exited"|"gone", "exit_code": int|None}."""
        ...

    async def logs(self, h: Handle, tail: int = 40) -> str:
        """Recent container output, for failure forensics."""
        ...

    async def cleanup(self, h: Handle) -> None:
        """Remove the container (unless keep_containers is set — a debugging aid)."""
        ...

    async def stop(self, h: Handle) -> None:
        """Force-remove a running container.

        Unlike `cleanup`, this ignores ``keep_containers`` — a cancelled run's
        container must die now, or the agent inside would keep working and push
        commits to the branch after the run was cancelled (breaking
        push-lands-or-retry: the branch must reflect exactly completed steps).
        """
        ...


class DockerRuntime:
    """launch()/status()/logs()/cleanup() over the docker daemon — everything above this
    runtime-agnostic. docker-py is synchronous; every call is wrapped in asyncio.to_thread
    (short socket I/O — fine off the event loop)."""

    def __init__(self, image: str, *, endpoint: str | None = None,
                 mem_limit: str = "4g", network: str = "bridge",
                 keep_containers: bool = False, env_forward: list[str] | None = None):
        import docker
        self.client = docker.DockerClient(base_url=endpoint) if endpoint else docker.from_env()
        self.image = image
        self.mem_limit = mem_limit
        self.network = network
        self.keep_containers = keep_containers
        self.env_forward = env_forward or []

    async def launch(self, run_id, step_id, attempt_no, env, *, context=None) -> Handle:
        def _launch():
            # Zero mounts (ADR-014): the container stages its result/progress/logs in
            # the image-owned /out dir and uploads them via the presigned PUT URLs in
            # `env`; the git clone lives in image-owned /workspace. The engine reads
            # everything back from object storage — no host dirs, no 0o777 trees, no
            # BB_WORKDIR path parity.
            container_env = dict(env)

            # Debugging knobs shared with the host — mock mode and CLAUDE_CODE tuning.
            # Credentials never travel through this path (they arrive in `env` from the
            # caller, resolved fresh from Secure Storage per launch).
            for k in self.env_forward:
                if os.environ.get(k):
                    container_env[k] = os.environ[k]

            log.info("launch step=%s attempt=%s image=%s", step_id, attempt_no, self.image)
            log.info("  result key: %s (no mounts — channels via object storage)",
                     result_key(run_id, step_id, attempt_no))
            try:
                c = self.client.containers.run(
                    self.image, detach=True, environment=container_env,
                    working_dir="/workspace",
                    mem_limit=self.mem_limit,
                    network_mode=self.network)
            except Exception:
                log.error("launch FAILED for step=%s:\n%s", step_id, traceback.format_exc())
                raise
            log.info("  container started id=%s", c.id[:12])
            return Handle(c.id, time.time(), run_id, step_id, attempt_no)
        return await asyncio.to_thread(_launch)

    async def make_handle(self, run_id, step_id, attempt_no, container_id, started_at):
        # Rebuild from persisted state. The channel keys derive from the same
        # (run_id, step_id, attempt_no) triple launch() used, so re-attach reads
        # the same objects — no disk state to reconstruct (ADR-014).
        return Handle(container_id, started_at, run_id, step_id, attempt_no)

    async def status(self, h: Handle) -> dict:
        def _status():
            import docker
            try:
                c = self.client.containers.get(h.container_id)
            except docker.errors.NotFound:
                return {"state": "gone", "exit_code": None}
            c.reload()
            if c.status == "running":
                return {"state": "running", "exit_code": None}
            return {"state": "exited", "exit_code": c.attrs.get("State", {}).get("ExitCode")}
        return await asyncio.to_thread(_status)

    async def logs(self, h: Handle, tail: int = 40) -> str:
        def _logs():
            try:
                return self.client.containers.get(h.container_id).logs(
                    tail=tail).decode("utf-8", "replace")
            except Exception:
                log.debug("container logs() failed for %s", h.container_id[:12],
                          exc_info=True)
                return ""
        return await asyncio.to_thread(_logs)

    async def cleanup(self, h: Handle) -> None:
        # keep_containers leaves containers around for post-mortem inspection
        # (docker exec / docker logs). They are ephemeral by design, so this is a
        # debugging aid only — remember to `docker container prune` afterwards.
        def _cleanup():
            if self.keep_containers:
                log.info("  keeping container %s for inspection (keep_containers=1)",
                         h.container_id[:12])
                return
            try:
                self.client.containers.get(h.container_id).remove(force=True)
            except Exception:
                log.debug("container remove failed for %s", h.container_id[:12],
                          exc_info=True)
        await asyncio.to_thread(_cleanup)

    async def stop(self, h: Handle) -> None:
        # Cancel path: the container must die regardless of keep_containers —
        # a live agent would keep working and could push to the run branch
        # after the run was cancelled.
        def _stop():
            try:
                self.client.containers.get(h.container_id).remove(force=True)
                log.info("  stopped container %s (run cancelled)", h.container_id[:12])
            except Exception:
                log.debug("container remove failed for %s", h.container_id[:12],
                          exc_info=True)
        await asyncio.to_thread(_stop)


async def _get_json(store, key: str) -> dict | None:
    """Latest payload at an object-store key. None on absence or corruption.

    Storage errors also read as absent: the exit-status channel still classifies
    the attempt (a down store means the agent's critical PUT failed too, and its
    non-zero exit routes the retry), so a transient read miss never lies about a
    result — it just defers to the next slow tick or the grace window."""
    if store is None:
        return None
    try:
        obj = await store.get(key)
    except Exception:
        log.debug("store.get(%s) failed", key, exc_info=True)
        return None
    if obj is None:
        return None
    try:
        return json.loads(obj.data)
    except (OSError, ValueError):  # corrupt payload → treat as absent
        return None


_COST_EVENT_RE = re.compile(r'"total_cost_usd"\s*:\s*(-?[0-9.]+)')


async def _scrape_partial_cost(store, agent_log_key: str) -> float | None:
    """Best-effort recovery of session spend from the agent-uploaded agent.log
    object when the container dies without publishing a result (cancel / timeout
    / OOM / failed_incomplete). The result event is the terminal stream-json
    line, so only the tail of the object is scanned and the LAST cost figure
    wins. The object is at most one heartbeat interval (~5s) stale — acceptable
    for a partial estimate. A session killed mid-flight usually has no cost
    event at all -> None — the caller records ``cost_reported: False`` rather
    than a confident zero.
    """
    if store is None:
        return None
    try:
        obj = await store.get(agent_log_key)
    except Exception:
        log.debug("store.get(%s) failed during cost scrape", agent_log_key, exc_info=True)
        return None
    if obj is None:
        return None
    data = obj.data[-8 * 1024 * 1024:].decode("utf-8", errors="replace")
    for raw in reversed(_COST_EVENT_RE.findall(data)):
        try:
            value = float(raw)
            if value >= 0:
                return value
        except ValueError:
            continue
    return None


CONTAINER_LOG_TAIL_LINES = 2000
CONTAINER_LOG_MAX_BYTES = 512 * 1024  # 512 KB cap on the captured container.log


async def _capture_container_log(store, runtime: Runtime, h: Handle) -> None:
    """Capture bounded container output into object storage as container.log
    while the container still exists — docker logs die with the container, and
    every terminal path (incl. cancel, whose stop() comes after) must read
    them before removal. Idempotent: an existing non-empty object (written by
    reconcile's own kill paths) wins. Best-effort — a capture failure never
    fails the step."""
    if store is None:
        return
    key = log_key(h.run_id, h.step_id, h.attempt_no, "container")
    try:
        head = await store.head(key)
        if head is not None and head.size > 0:
            return
    except Exception:
        log.debug("store.head(%s) failed — skipping container.log capture", key,
                  exc_info=True)
        return
    logs = await runtime.logs(h, tail=CONTAINER_LOG_TAIL_LINES)
    if not logs:
        return
    data = logs.encode("utf-8", "replace")[-CONTAINER_LOG_MAX_BYTES:]
    try:
        await store.put(key, data, content_type="text/plain")
    except Exception:
        log.warning("could not upload container.log for container %s",
                    h.container_id[:12], exc_info=True)


async def reconcile(runtime: Runtime, h: Handle, deadline_s: float,
                    on_progress=None, *, cancel_event: asyncio.Event | None = None,
                    store=None) -> dict:
    """Poll until terminal, then classify by joining result + exit status.

    on_progress(dict) is awaited when the container's progress changes, so a
    long-running stage reports liveness instead of going silent for minutes.

    Two signals on two channels (ADR-014): the result payload / progress /
    agent.log arrive via object storage (agent presigned PUTs), while the exit
    status is polled from the runtime. The storage channels are read at the
    SLOW_POLL_TICKS cadence — liveness needs seconds, not sub-second freshness —
    and the status poll stays at POLL_INTERVAL.

    cancel_event: when set, abort immediately with the CANCELLED sentinel — the
    caller (RunDriver) owns killing the container via runtime.stop(). POLL_INTERVAL
    is sub-second, so a stop-request lands within one poll tick.
    """
    exited_at = None
    polls = 0
    last_progress = None
    res_key = result_key(h.run_id, h.step_id, h.attempt_no)
    prog_key = progress_key(h.run_id, h.step_id, h.attempt_no)
    agent_log_key = log_key(h.run_id, h.step_id, h.attempt_no, "agent")
    payload = None
    log.info("reconcile start: watching container=%s deadline=%ss result_key=%s",
             h.container_id[:12], deadline_s, res_key)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            log.warning("reconcile aborted — cancel event set (run cancelled)")
            # The kill lands mid-session: whatever the CLI reported before the
            # stop is recoverable from its log and must still count.
            partial = await _scrape_partial_cost(store, agent_log_key)
            # The caller's stop() removes the container right after this
            # returns — this is the last moment its output is readable.
            await _capture_container_log(store, runtime, h)
            return {"status": CANCELLED,
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}
        polls += 1
        try:
            st = await runtime.status(h)
        except Exception:  # noqa: BLE001 — any status() failure classifies as infra failure
            log.error("status() raised — treating as infra failure:\n%s",
                      traceback.format_exc())
            return {"status": Result.FAILED_INFRA, "reason": "runtime status() error"}
        # Storage channels on the slow cadence (first poll always reads so a
        # fast-completing step is seen).
        if polls == 1 or polls % SLOW_POLL_TICKS == 0:
            payload = await _get_json(store, res_key)

            # container heartbeat — surfaces "still working" instead of dead air
            prog = await _get_json(store, prog_key)
            if prog and prog != last_progress:
                last_progress = prog
                log.info("  progress: phase=%s %s (%ss)", prog.get("phase"),
                         prog.get("note", ""), prog.get("elapsed_s"))
                if on_progress:
                    try:
                        await on_progress(prog)
                    except Exception:
                        log.debug("progress publish failed", exc_info=True)
        elapsed = time.time() - h.started_at

        if polls == 1 or polls % 10 == 0 or st["state"] != "running":
            log.info("  poll #%d: state=%s exit=%s result_present=%s elapsed=%.1fs",
                     polls, st["state"], st.get("exit_code"),
                     payload is not None, elapsed)

        if st["state"] == "gone":
            log.warning("container gone without result -> failed_infra")
            partial = await _scrape_partial_cost(store, agent_log_key)
            return {"status": Result.FAILED_INFRA,
                    "reason": "container vanished (OOM / host lost)",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if st["state"] == "exited":
            exited_at = exited_at or time.time()
            # The agent uploads its result in the EXIT trap before docker
            # reports exited (S3 is read-after-write consistent), but re-read
            # each exited tick during the grace window anyway — a stale slow-
            # tick None must not wait out the grace on a misread.
            if payload is None and store is not None:
                payload = await _get_json(store, res_key)
            if payload:
                status = payload.get("status", Result.COMPLETED)
                if st["exit_code"] not in (0, None) and status == Result.COMPLETED:
                    status = Result.FAILED_EXECUTION
                log.info("  -> classified '%s' (exit=%s)", status, st.get("exit_code"))
                cost_usd = float(payload.get("cost_usd") or 0)
                return {"status": status,
                        "cost_usd": cost_usd,
                        # Old agent results predate the flag: infer it from a
                        # non-zero number so real spend never reads "unknown".
                        "cost_reported": bool(payload.get("cost_reported", cost_usd > 0)),
                        "cost_partial": bool(payload.get("cost_partial")),
                        "next_hint": payload.get("next"),
                        "artifact": payload.get("artifact"),
                        "summary": payload.get("summary"),
                        "summary_full": payload.get("summary_full"),
                        "files": payload.get("files") or [],
                        # What the skill wants a human to actually review — a curated subset
                        # (or superset with context files), each optionally annotated. When a
                        # skill emits this, the UI shows these by default instead of every
                        # git-touched file. Absent -> UI falls back to `files`.
                        "review_files": payload.get("review_files") or [],
                        "commit": payload.get("commit"),
                        "reason": payload.get("reason")}
            if time.time() - exited_at < GRACE_SECONDS:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            log.warning("  exited (exit=%s) but NO %s at %s -> failed_incomplete",
                        st.get("exit_code"), RESULT_FILENAME, res_key)
            partial = await _scrape_partial_cost(store, agent_log_key)
            return {"status": Result.FAILED_INCOMPLETE,
                    "reason": f"exited ({st['exit_code']}) without publishing a result",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if elapsed > deadline_s:
            log.warning("  deadline exceeded (%.1fs > %ss) -> failed_timeout",
                        elapsed, deadline_s)
            partial = await _scrape_partial_cost(store, agent_log_key)
            # This branch is the only one where reconcile itself kills the
            # container — capture its output before cleanup() removes it.
            await _capture_container_log(store, runtime, h)
            await runtime.cleanup(h)
            return {"status": Result.FAILED_TIMEOUT,
                    "reason": f"exceeded {deadline_s}s deadline",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}
        await asyncio.sleep(POLL_INTERVAL)


def _outbox_reply(reply: dict | None, expected_seq: int) -> dict | None:
    """The completed-turn dict when an outbox object answers ``expected_seq``.

    Exact seq match — the outbox at one attempt's key is overwritten per turn,
    and a mismatched seq means the container has not seen this turn yet (keep
    polling). None = not the turn we are waiting for.
    """
    if reply is None or reply.get("seq") != expected_seq:
        return None
    cost_usd = float(reply.get("cost_usd") or 0)
    return {"status": Result.COMPLETED,
            "response": reply.get("response", ""),
            "cost_usd": cost_usd,
            "cost_reported": bool(reply.get("cost_reported", cost_usd > 0)),
            "cost_partial": bool(reply.get("cost_partial")),
            "commit": reply.get("commit"),
            "files": reply.get("files") or []}


async def reconcile_turn(runtime: Runtime, h: Handle, expected_seq: int,
                         deadline_s: float, *, turn_started_at: float | None = None,
                         cancel_event: asyncio.Event | None = None,
                         store=None) -> dict:
    """Poll a LIVE session container until it answers turn ``expected_seq`` (ADR-016 §2).

    The counterpart to reconcile() for ad-hoc session turns. Unlike reconcile(),
    the happy path does NOT kill the container — the session lives on for the
    next turn, so the caller must leave the handle valid. Terminal conditions:

      - outbox at ``turns/<run>/<step>/<attempt>/outbox.json`` with
        ``seq == expected_seq`` → completed with the turn's reply fields;
      - cancel_event → CANCELLED sentinel (caller owns runtime.stop());
      - container gone → failed_infra; exited without a matching reply after
        the grace window → failed_incomplete (the caller cold-starts a fresh
        incarnation for the next turn);
      - deadline exceeded → failed_timeout + cleanup (a hung turn is stuck
        work — the container is removed like any timed-out step).

    ``turn_started_at`` (default: the incarnation's launch time) anchors the
    deadline math: a long-lived container answering turn N+1 must get a full
    per-turn deadline, not the remainder of its incarnation's.
    """
    exited_at = None
    polls = 0
    turn_start = turn_started_at if turn_started_at is not None else h.started_at
    outbox_key = turn_outbox_key(h.run_id, h.step_id, h.attempt_no)
    agent_log_key = log_key(h.run_id, h.step_id, h.attempt_no, "agent")
    log.info("reconcile_turn start: container=%s seq=%s deadline=%ss outbox=%s",
             h.container_id[:12], expected_seq, deadline_s, outbox_key)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            log.warning("reconcile_turn aborted — cancel event set (run cancelled)")
            partial = await _scrape_partial_cost(store, agent_log_key)
            # The caller's stop() removes the container right after this
            # returns — this is the last moment its output is readable.
            await _capture_container_log(store, runtime, h)
            return {"status": CANCELLED,
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}
        polls += 1
        try:
            st = await runtime.status(h)
        except Exception:  # noqa: BLE001 — any status() failure classifies as infra failure
            log.error("status() raised — treating as infra failure:\n%s",
                      traceback.format_exc())
            return {"status": Result.FAILED_INFRA, "reason": "runtime status() error"}

        # The outbox on the slow cadence (first poll always reads so a fast
        # turn is seen; the live container's reply is the only terminal signal).
        reply = None
        if polls == 1 or polls % SLOW_POLL_TICKS == 0:
            reply = await _get_json(store, outbox_key)

        elapsed = time.time() - turn_start

        if polls == 1 or polls % 10 == 0 or st["state"] != "running":
            log.info("  poll #%d: state=%s exit=%s seq_match=%s elapsed=%.1fs",
                     polls, st["state"], st.get("exit_code"),
                     reply is not None and reply.get("seq") == expected_seq, elapsed)

        match = _outbox_reply(reply, expected_seq)
        if match is not None:
            log.info("  -> turn %s answered (container stays up)", expected_seq)
            return match

        if st["state"] == "gone":
            log.warning("container gone mid-turn -> failed_infra")
            partial = await _scrape_partial_cost(store, agent_log_key)
            return {"status": Result.FAILED_INFRA,
                    "reason": "container vanished (OOM / host lost)",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if st["state"] == "exited":
            exited_at = exited_at or time.time()
            # The agent PUTs the outbox in its exit trap before docker reports
            # exited — re-read each exited tick during the grace window.
            if reply is None and store is not None:
                match = _outbox_reply(await _get_json(store, outbox_key), expected_seq)
                if match is not None:
                    log.info("  -> turn %s answered on the exit tick", expected_seq)
                    return match
            if time.time() - exited_at < GRACE_SECONDS:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            log.warning("  exited (exit=%s) without answering turn %s -> failed_incomplete",
                        st.get("exit_code"), expected_seq)
            partial = await _scrape_partial_cost(store, agent_log_key)
            return {"status": Result.FAILED_INCOMPLETE,
                    "reason": f"exited ({st['exit_code']}) without answering turn {expected_seq}",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if elapsed > deadline_s:
            log.warning("  turn deadline exceeded (%.1fs > %ss) -> failed_timeout",
                        elapsed, deadline_s)
            partial = await _scrape_partial_cost(store, agent_log_key)
            await _capture_container_log(store, runtime, h)
            await runtime.cleanup(h)
            return {"status": Result.FAILED_TIMEOUT,
                    "reason": f"turn exceeded {deadline_s}s deadline",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}
        await asyncio.sleep(POLL_INTERVAL)
