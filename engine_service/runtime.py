"""Runtime protocol + DockerRuntime — the pluggable step-execution backend (ADR-013 §4).

The Runtime protocol is the seam between the engine state machine and whatever actually
runs step containers. `DockerRuntime` serves local dev (docker-compose); a FargateRuntime
behind the same protocol is the production variant (deferred).

Design notes carried forward from the R&D engine (engine.py):
  - Two independent signals: the result payload (written by the container to /out) and the
    exit status (polled from the runtime). A crashed container cannot report its own death.
  - The reconciler joins those signals against a deadline to classify each attempt.
  - Host mounts must be 0o777: the agent container runs as a NON-ROOT user (Claude Code
    requires it for --dangerously-skip-permissions) and must be able to publish results.
"""

import asyncio
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# The orchestrator's control-plane result file. Deliberately NOT "result.json":
# the PDLC skills use result.json as their own in-repo handoff artifact, and an agent
# will happily overwrite a file by that name. Keep the control plane in its own namespace.
RESULT_FILENAME = "bb_step_result.json"
GRACE_SECONDS = 3.0
POLL_INTERVAL = 0.4

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
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Handle:
    """A launched step container — everything needed to poll and clean it up."""
    container_id: str
    result_path: Path
    started_at: float


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
        """Launch one step container. `env` is the fully composed bundle (ADR-013 §5);
        `context` (the per-run context dict) rides in as BB_CONTEXT — the runner
        writes CONTEXT_FILE inside the container (no /ctx mount in Phase 1)."""
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

    def __init__(self, image: str, *, endpoint: str | None = None, workdir: str,
                 mem_limit: str = "4g", network: str = "bridge",
                 keep_containers: bool = False, env_forward: list[str] | None = None):
        import docker
        self.client = docker.DockerClient(base_url=endpoint) if endpoint else docker.from_env()
        self.image = image
        self.workdir = Path(workdir)
        self.mem_limit = mem_limit
        self.network = network
        self.keep_containers = keep_containers
        self.env_forward = env_forward or []

    async def launch(self, run_id, step_id, attempt_no, env, *, context=None) -> Handle:
        def _launch():
            # The container runs as a NON-ROOT user (so Claude Code will accept
            # --dangerously-skip-permissions). Host-created mounts must therefore be
            # writable by that user, or the container can't publish its result.
            outdir = self.workdir / "results" / run_id / step_id / str(attempt_no)
            outdir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(outdir, 0o777)
            except OSError:
                log.debug("chmod %s 0o777 failed", outdir, exc_info=True)

            # Fresh launch: drop any result a previous attempt of this step left behind.
            # The reconciler reads this file's presence as "the container published" — a
            # stale one (re-loop into a reused attempt dir, or a relaunch whose container
            # died before publishing) would make poll #1 report result_present=True and
            # could classify this attempt with the previous attempt's payload.
            stale = outdir / RESULT_FILENAME
            if stale.exists():
                stale.unlink()

            # Git mode: the container clones the run branch (created by the ENGINE at
            # init, ADR-013 §2) into a fresh empty host dir — so the host can still read
            # artifacts back from it after the run.
            workspace = self.workdir / "clones" / run_id / step_id / str(attempt_no)
            workspace.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(workspace, 0o777)
            except OSError:
                log.debug("chmod %s 0o777 failed", workspace, exc_info=True)

            # Per-run CONTEXT travels as the compact BB_CONTEXT env copy only
            # (Phase 1 dropped the /ctx bind mount) — the runner writes
            # CONTEXT_FILE itself inside the container. `context` stays on the
            # protocol so scripted runtimes (FakeRuntime) can record it.
            container_env = dict(env)

            # Debugging knobs shared with the host — mock mode and CLAUDE_CODE tuning.
            # Credentials never travel through this path (they arrive in `env` from the
            # caller, resolved fresh from Secure Storage per launch).
            for k in self.env_forward:
                if os.environ.get(k):
                    container_env[k] = os.environ[k]

            # Phase 1: only /out (result payload) and /workspace (git clone) are
            # mounted. Skills arrive via BB_SKILL_URL download inside the
            # container; context via BB_CONTEXT.
            vols = {str(outdir): {"bind": "/out", "mode": "rw"},
                    str(workspace): {"bind": "/workspace", "mode": "rw"}}

            log.info("launch step=%s attempt=%s image=%s", step_id, attempt_no, self.image)
            log.info("  result path (host): %s", outdir / RESULT_FILENAME)
            log.info("  mounts: %s", {k: v["bind"] for k, v in vols.items()})
            try:
                c = self.client.containers.run(
                    self.image, detach=True, environment=container_env, volumes=vols,
                    working_dir="/workspace",
                    mem_limit=self.mem_limit,
                    network_mode=self.network)
            except Exception:
                log.error("launch FAILED for step=%s:\n%s", step_id, traceback.format_exc())
                raise
            log.info("  container started id=%s", c.id[:12])
            return Handle(c.id, outdir / RESULT_FILENAME, time.time())
        return await asyncio.to_thread(_launch)

    async def make_handle(self, run_id, step_id, attempt_no, container_id, started_at):
        # Reconstruct the result path from the SAME layout launch() writes into —
        # the container's /out maps to workdir/results/<run>/<step>/<attempt>.
        return Handle(
            container_id,
            self.workdir / "results" / str(run_id) / step_id / str(attempt_no) / RESULT_FILENAME,
            started_at,
        )

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


async def read_result(path: Path) -> dict | None:
    """Read a step's published result payload. None on absence or corruption."""
    def _read():
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):  # missing/corrupt payload → treat as absent
            return None
    return await asyncio.to_thread(_read)


_COST_EVENT_RE = re.compile(r'"total_cost_usd"\s*:\s*(-?[0-9.]+)')


async def _scrape_partial_cost(log_path: Path) -> float | None:
    """Best-effort recovery of session spend from ``agent.log`` when the
    container dies without publishing a result (cancel / timeout / OOM /
    failed_incomplete). The result event is the terminal stream-json line, so
    only the tail of the file is scanned and the LAST cost figure wins. A
    session killed mid-flight usually has no cost event at all -> None — the
    caller records ``cost_reported: False`` rather than a confident zero.
    """
    def _read() -> float | None:
        try:
            if not log_path.exists():
                return None
            with open(log_path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 8 * 1024 * 1024))
                data = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        for raw in reversed(_COST_EVENT_RE.findall(data)):
            try:
                value = float(raw)
                if value >= 0:
                    return value
            except ValueError:
                continue
        return None
    return await asyncio.to_thread(_read)


CONTAINER_LOG_TAIL_LINES = 2000
CONTAINER_LOG_MAX_BYTES = 512 * 1024  # 512 KB cap on the captured container.log


async def _dump_container_log(runtime: Runtime, h: Handle) -> None:
    """Capture bounded container output into the attempt dir as container.log
    while the container still exists — docker logs die with the container, and
    every terminal path (incl. cancel, whose stop() comes after) must read
    them before removal. Idempotent: an existing non-empty capture (written
    by reconcile's own kill paths) wins."""
    target = h.result_path.parent / "container.log"
    if target.exists() and target.stat().st_size > 0:
        return
    logs = await runtime.logs(h, tail=CONTAINER_LOG_TAIL_LINES)
    if not logs:
        return
    data = logs.encode("utf-8", "replace")[-CONTAINER_LOG_MAX_BYTES:]
    try:
        target.write_bytes(data)
    except OSError:
        log.warning("could not write container.log for container %s",
                    h.container_id[:12])


async def reconcile(runtime: Runtime, h: Handle, deadline_s: float,
                    on_progress=None, *, cancel_event: asyncio.Event | None = None) -> dict:
    """Poll until terminal, then classify by joining result + exit status.

    on_progress(dict) is awaited when the container's progress.json changes, so a
    long-running stage reports liveness instead of going silent for minutes.

    cancel_event: when set, abort immediately with the CANCELLED sentinel — the
    caller (RunDriver) owns killing the container via runtime.stop(). POLL_INTERVAL
    is sub-second, so a stop-request lands within one poll tick.
    """
    exited_at = None
    polls = 0
    last_progress = None
    progress_path = h.result_path.parent / "progress.json"
    log.info("reconcile start: watching container=%s deadline=%ss result=%s",
             h.container_id[:12], deadline_s, h.result_path)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            log.warning("reconcile aborted — cancel event set (run cancelled)")
            # The kill lands mid-session: whatever the CLI reported before the
            # stop is recoverable from its log and must still count.
            partial = await _scrape_partial_cost(h.result_path.parent / "agent.log")
            # The caller's stop() removes the container right after this
            # returns — this is the last moment its output is readable.
            await _dump_container_log(runtime, h)
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
        payload = await read_result(h.result_path)
        elapsed = time.time() - h.started_at

        # container heartbeat — surfaces "still working" instead of dead air
        prog = await read_result(progress_path)
        if prog and prog != last_progress:
            last_progress = prog
            log.info("  progress: phase=%s %s (%ss)", prog.get("phase"),
                     prog.get("note", ""), prog.get("elapsed_s"))
            if on_progress:
                try:
                    await on_progress(prog)
                except Exception:
                    log.debug("progress publish failed", exc_info=True)

        if polls == 1 or polls % 10 == 0 or st["state"] != "running":
            log.info("  poll #%d: state=%s exit=%s result_present=%s elapsed=%.1fs",
                     polls, st["state"], st.get("exit_code"),
                     payload is not None, elapsed)

        if st["state"] == "gone":
            log.warning("container gone without result -> failed_infra")
            partial = await _scrape_partial_cost(h.result_path.parent / "agent.log")
            return {"status": Result.FAILED_INFRA,
                    "reason": "container vanished (OOM / host lost)",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if st["state"] == "exited":
            exited_at = exited_at or time.time()
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
                        st.get("exit_code"), RESULT_FILENAME, h.result_path)
            partial = await _scrape_partial_cost(h.result_path.parent / "agent.log")
            return {"status": Result.FAILED_INCOMPLETE,
                    "reason": f"exited ({st['exit_code']}) without publishing a result",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}

        if elapsed > deadline_s:
            log.warning("  deadline exceeded (%.1fs > %ss) -> failed_timeout",
                        elapsed, deadline_s)
            partial = await _scrape_partial_cost(h.result_path.parent / "agent.log")
            # This branch is the only one where reconcile itself kills the
            # container — capture its output before cleanup() removes it.
            await _dump_container_log(runtime, h)
            await runtime.cleanup(h)
            return {"status": Result.FAILED_TIMEOUT,
                    "reason": f"exceeded {deadline_s}s deadline",
                    "cost_usd": partial or 0,
                    "cost_reported": partial is not None,
                    "cost_partial": partial is not None}
        await asyncio.sleep(POLL_INTERVAL)
