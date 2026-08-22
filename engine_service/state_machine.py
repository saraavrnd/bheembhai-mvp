"""Run state machine — drives a run from persisted state to its next pause (ADR-003/013).

Lifecycle model (work-item = dispatch token):
  - One dispatch advances the run until the next pause — a policy gate
    (`run.state = "paused"`) or a terminal state — and returns. The worker marks
    the item `done`; a `continue` item (payload action: approve/send_back/resume)
    drives the next segment.
  - Everything the machine needs survives in the DB (`runs.state`,
    `runs.current_step`, `steps.exec_state`, `steps.fargate_task_arn`,
    `transitions.payload`). A crash mid-dispatch is healed by ADR-003 recovery +
    idempotent resume — never by replaying from memory. Key consequence: step
    completion, routing, and the gate pause are committed ATOMICALLY, so
    `current_step` always points at the next unrun step (or the gated one) after
    any commit — a resume can never double-run or skip a gate.
  - Visit counting is per-dispatch: a loop that crosses a gate pauses for a human
    each cycle (not a runaway); a loop that doesn't cross a pause is capped
    in-dispatch (`max_step_visits`).

Ported from the R&D engine (engine.py _loop/_run_step) with the approval Event
replaced by the DB pause + continue-item flow.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from bheembhai.log_keys import (
    log_key,
    progress_key,
    result_key,
    session_transcript_key,
    turn_inbox_key,
    turn_outbox_key,
)
from bheembhai.models.run import Run, Step, Transition
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from engine_service.contexts import build_env_bundle, build_step_context
from engine_service.log_upload import register_logs_at_launch, upload_step_logs
from engine_service.persistence import (
    RUN_LEVEL_ATTEMPT,
    RUN_LEVEL_STEP,
    record_transition,
)
from engine_service.run_init import init_run
from engine_service.runtime import (
    CANCELLED,
    POLL_INTERVAL,
    Handle,
    _capture_container_log,
    reconcile,
    reconcile_turn,
)
from engine_service.workflow import (
    TRANSIENT,
    ExecState,
    Result,
    WorkflowSpec,
)

logger = logging.getLogger(__name__)

DONE = "DONE"
TERMINAL_STATES = {"completed", "failed", "cancelled"}
MAX_ITERATIONS = 40     # hard seatbelt on the routing loop itself


# ── Pure routing helpers ────────────────────────────────────────────────

def route_next(workflow_spec: WorkflowSpec, step_id: str, status: str,
               outcome: dict) -> str | None:
    """Backend-authoritative routing (engine.py _loop port). A skill's `next`
    hint is advisory unless the workflow explicitly says `route_to` for that
    status; otherwise the workflow's `on:` map is the only authority."""
    hint = outcome.get("next_hint")
    if hint and workflow_spec.route_for(step_id, status) == "route_to":
        return hint
    return workflow_spec.route_for(step_id, status)


def steps_after(workflow_spec: WorkflowSpec, target: str) -> list[str]:
    """Step ids that come after `target` in workflow order — the send_back reset set."""
    ids = list(workflow_spec.steps)
    if target not in ids:
        return []
    return ids[ids.index(target) + 1:]


def _handoff_for(outcome: dict, from_step: str, status: str) -> dict | None:
    """The non-happy-verdict report handed to the next step (self-loop guarded
    in build_step_context)."""
    if status == Result.COMPLETED:
        return None
    return {
        "from_step": from_step,
        "status": status,
        "summary": outcome.get("summary"),
        # Key must match run_skill.sh's jq path `.upstream_handoff.report_files`
        # — a `reports`/`report_files` mismatch silently dropped the
        # "Read its report first" clause from the next step's prompt.
        "report_files": outcome.get("review_files") or outcome.get("files") or [],
    }


def _env_int(ctx, name: str, default: int) -> int:
    """Read an engine guardrail knob from the run's resolved env vars.

    The platform validates tunables as int ≥ 1 at save time, so a non-int here
    means the row predates validation or was hand-inserted — fall back to the
    engine default rather than trusting it. Clamped to ≥ 1 either way.
    """
    try:
        return max(1, int(ctx.env_vars.get(name, default)))
    except (TypeError, ValueError):
        return max(1, default)


def _gate_card(gate: dict, outcome: dict) -> dict:
    """The reviewer-facing card, stored on the awaiting_approval transition so a
    gate survives engine restarts (ADR-003) and approve can re-route from it."""
    return {
        "role": gate.get("role"),
        "summary": outcome.get("summary"),
        "artifact": outcome.get("artifact"),
        "result_status": outcome.get("status"),
        "reason": outcome.get("reason"),
        "files": outcome.get("files") or [],
        "review_files": outcome.get("review_files") or [],
        "next_hint": outcome.get("next_hint"),
        "commit": outcome.get("commit"),
        "cost_usd": outcome.get("cost_usd"),
        "cost_reported": bool(outcome.get("cost_reported")),
        "cost_partial": bool(outcome.get("cost_partial")),
    }


# ── DB helpers ──────────────────────────────────────────────────────────

async def _get_step(session: AsyncSession, run_id, step_id: str) -> Step | None:
    res = await session.execute(
        select(Step).where(Step.run_id == run_id, Step.step_id == step_id))
    return res.scalar_one_or_none()


async def _last_gate_transition(session: AsyncSession, run_id) -> Transition | None:
    res = await session.execute(
        select(Transition)
        .where(Transition.run_id == run_id,
               Transition.to_state == ExecState.AWAITING_APPROVAL)
        .order_by(Transition.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def _fail_run(session: AsyncSession, run: Run, *, reason: str,
                    result_status: str | None = None,
                    step_id: str = RUN_LEVEL_STEP,
                    attempt_no: int = RUN_LEVEL_ATTEMPT) -> None:
    prev = run.state
    run.state = "failed"
    record_transition(session, run.id, prev, "failed",
                      step_id=step_id, attempt_no=attempt_no,
                      result_status=result_status, reason=reason)
    await session.commit()


async def _cancel_run(session: AsyncSession, run: Run, *, reason: str,
                      step_id: str = RUN_LEVEL_STEP,
                      attempt_no: int = RUN_LEVEL_ATTEMPT,
                      publish=None) -> None:
    """Record the run as cancelled (stop-run). Commits atomically with any
    step-level transition the caller already recorded in this session."""
    prev = run.state
    run.state = "cancelled"
    record_transition(session, run.id, prev, "cancelled",
                      step_id=step_id, attempt_no=attempt_no, reason=reason)
    await session.commit()
    await _publish(publish, {"type": "run_cancelled", "run_id": str(run.id)})


async def _publish(publish, event: dict) -> None:
    if publish is None:
        return
    try:
        await publish(event)
    except Exception:
        logger.exception("publish failed (non-fatal)")


# ── Dispatch entry ──────────────────────────────────────────────────────

async def drive_run(session: AsyncSession, item, config, runtime,
                    secure_storage, *, publish=None,
                    cancel_event: asyncio.Event | None = None,
                    store=None) -> None:
    """Advance the run one dispatch. The item's state transitions are the
    worker's job — this never touches them.

    cancel_event (stop-run): set by the worker's cancel handler — the loop
    aborts at the next checkpoint instead of starting new work.

    store: the ObjectStorage backend (ADR-011) that receives each attempt's
    logs. None disables upload (tests, minimal deployments)."""
    run = await session.get(Run, item.run_id)
    if run is None or run.state in TERMINAL_STATES:
        return

    payload = item.payload or {}
    action = payload.get("action") if item.action == "continue" else "start"

    # Idempotent init first (ADR-013 §2 / ADR-003): on a fresh run this creates
    # the branch + step rows + flips pending→running; on resume it is a cheap
    # reload of workflow/policy/integrations + fresh credential resolution.
    ctx = await init_run(session, run.id, config, secure_storage, store=store)

    start: str | None = None
    reviewer_feedback = ""
    handoff: dict | None = None

    # Ad-hoc sessions (ADR-016 §2) have no gates and no routing: one dispatch =
    # one turn = one pause (awaiting_input). A `turn`/`end` continue item drives
    # the session; approve/send_back/resume are foreign vocabulary — keep
    # waiting, nothing re-runs. A crash re-delivery of the ORIGINAL turn item
    # (stale-claim reaper) lands here with the query still in its payload.
    if run.run_kind == "adhoc":
        if run.state == "paused":
            if action == "turn":
                await _session_turn(session, ctx, config, runtime,
                                    query=str(payload.get("query") or ""),
                                    publish=publish, cancel_event=cancel_event,
                                    store=store)
                return
            if action == "end":
                await _end_session(session, ctx, config, runtime,
                                   publish=publish, cancel_event=cancel_event,
                                   store=store)
                return
            return    # approve/send_back/resume/stale start — no gate here
        # pending (fresh start: first turn = the run's query) or running (crash
        # re-delivery of a turn item — its query is in the payload).
        if action == "end":
            return    # defensive: end only advances from awaiting_input
        await _session_turn(session, ctx, config, runtime,
                            query=str(payload.get("query") or "") or run.user_query or "",
                            publish=publish, cancel_event=cancel_event,
                            store=store)
        return

    if run.state == "paused":
        if action == "approve":
            start, reviewer_feedback, handoff = await _apply_approve(session, ctx, payload)
        elif action == "send_back":
            start, reviewer_feedback = await _apply_send_back(session, ctx, payload)
        else:
            # resume / a stale `start` re-claimed after a crash: the gate is still
            # open — re-notify and keep waiting. Nothing re-runs.
            await _renotify_gate(session, ctx, publish)
            return
        if not start:
            return    # decision ended the run (no route) — already recorded
    else:
        # pending (fresh start) or running (crash resume): resume from persisted
        # position — current_step always points at the next unrun step after any
        # commit, or the mid-flight one.
        start = run.current_step or ctx.workflow_spec.start

    await _loop(session, ctx, config, runtime, start=start,
                reviewer_feedback=reviewer_feedback, handoff=handoff,
                publish=publish, cancel_event=cancel_event, store=store)


# ── The step loop ───────────────────────────────────────────────────────

async def _loop(session: AsyncSession, ctx, config, runtime, *, start: str,
                reviewer_feedback: str = "", handoff: dict | None = None,
                publish=None,
                cancel_event: asyncio.Event | None = None,
                store=None) -> None:
    """Port of engine.py _loop: run steps, route on verdicts, hand off non-happy
    results — with per-dispatch visit caps and DB-pause gates."""
    wf_spec = ctx.workflow_spec
    run = ctx.run
    step_id = start
    visits: dict[str, int] = {}
    iterations = 0

    while True:
        # Stop-run checkpoint: the cancel event fires between steps — do not
        # route into the next step, and do not leave the run mid-flight.
        if cancel_event is not None and cancel_event.is_set():
            await _cancel_run(session, run,
                              reason="cancelled between steps (stop requested)",
                              publish=publish)
            return

        # Cross-engine safety: a cancel handler in another engine process has no
        # in-memory event to signal — it writes runs.state directly. The DB is
        # authoritative: never route into a new step of a cancelled run.
        db_state = await session.scalar(select(Run.state).where(Run.id == run.id))
        if db_state == "cancelled":
            await _cancel_run(session, run,
                              reason="cancelled by stop request (observed from DB)",
                              publish=publish)
            return

        iterations += 1
        if iterations > MAX_ITERATIONS:
            await _fail_run(session, run,
                            reason=f"workflow loop exceeded {MAX_ITERATIONS} iterations — routing map likely cyclic")
            return

        if step_id == DONE:
            run.state = "completed"
            record_transition(session, run.id, "running", "completed",
                              reason="workflow finished — all steps done")
            await session.commit()
            await _publish(publish, {"type": "run_completed", "run_id": str(run.id)})
            return

        spec = wf_spec.steps.get(step_id)
        if spec is None:
            await _fail_run(session, run,
                            reason=f"workflow routed to unknown step '{step_id}'")
            return

        # Visit cap (per dispatch — a gate pauses between cycles, so this only
        # fires on loops that never reach a human).
        visits[step_id] = visits.get(step_id, 0) + 1
        cap = _env_int(ctx, "BB_MAX_STEP_VISITS", config.engine.max_step_visits)
        if visits[step_id] > cap:
            await _fail_run(session, run, step_id=step_id,
                            reason=f"step '{step_id}' visited {visits[step_id]} times in one "
                                   f"dispatch (cap {cap}) — runaway loop halted, escalating for a human")
            return

        verdict, outcome = await _run_step(
            session, ctx, config, runtime, step_id, spec,
            reviewer_feedback=reviewer_feedback, handoff=handoff, publish=publish,
            cancel_event=cancel_event, store=store)
        if verdict != "advanced":
            return    # paused at a gate, run failed, or cancelled — dispatch ends here
        reviewer_feedback = ""    # consumed by the step that just ran
        ran_step = step_id
        step_id = run.current_step    # set atomically with the completion
        handoff = _handoff_for(outcome, ran_step, outcome.get("status"))


# ── One step: launch / reconcile / classify / route ─────────────────────

async def _launch_upload_contract(store, run_id: str, step_id: str, attempt_no: int,
                                  deadline: float) -> dict[str, str]:
    """Presign the four PUT URLs for one launch (ADR-014): the agent uploads its
    result payload, progress, agent.log, and diagnostics to deterministic keys —
    zero mounts. Raises on presign failure (the caller retries as failed_infra).
    Entries a backend declines (None — LocalStorage) are omitted, and the agent
    skips those uploads. URLs are bearer credentials: log keys, never URLs.
    """
    # The critical result PUT happens at exit — cover the full deadline plus
    # slack (heartbeat PUTs after expiry degrade to stale progress, harmless).
    expires_in = max(3600, int(deadline) + 600)
    out: dict[str, str] = {}
    for env_name, key in (
        ("BB_RESULT_PUT_URL", result_key(run_id, step_id, attempt_no)),
        ("BB_PROGRESS_PUT_URL", progress_key(run_id, step_id, attempt_no)),
        ("BB_LOG_PUT_URL", log_key(run_id, step_id, attempt_no, "agent")),
        ("BB_DIAG_PUT_URL", log_key(run_id, step_id, attempt_no, "diagnostics")),
    ):
        presigned = await store.presigned_put_url(key, expires_in=expires_in)
        if presigned is not None:
            out[env_name] = presigned.url
    return out


async def _launch_turn_contract(store, run_id: str, step_id: str,
                                attempt_no: int,
                                session_id: str | None = None,
                                resume: bool = False) -> dict[str, str]:
    """Presign the session channels for one launch (ADR-016 §2-3): one GET
    (the inbox — the container polls the SAME key for its whole life) and one
    PUT (the outbox — overwritten per turn, seq-matched), plus the transcript
    channels when a session id is known (PUT always — every graceful exit
    uploads; GET only when this incarnation RESUMES). Long expiry: a live
    session container outlives any step deadline. Entries a backend declines
    are omitted (LocalStorage)."""
    out: dict[str, str] = {}
    inbox = await store.presigned_get_url(
        turn_inbox_key(run_id, step_id, attempt_no), expires_in=86400)
    if inbox is not None:
        out["BB_INBOX_GET_URL"] = inbox.url
    outbox = await store.presigned_put_url(
        turn_outbox_key(run_id, step_id, attempt_no), expires_in=86400)
    if outbox is not None:
        out["BB_OUTBOX_PUT_URL"] = outbox.url
    if session_id:
        transcript_key = session_transcript_key(run_id, session_id)
        put = await store.presigned_put_url(transcript_key, expires_in=86400)
        if put is not None:
            out["BB_TRANSCRIPT_PUT_URL"] = put.url
        if resume:
            get = await store.presigned_get_url(transcript_key, expires_in=86400)
            if get is not None:
                out["BB_TRANSCRIPT_GET_URL"] = get.url
    return out


async def _clear_attempt_channels(store, run_id: str, step_id: str,
                                  attempt_no: int) -> None:
    """Delete any objects already sitting at this attempt's deterministic keys
    (ADR-014) before a fresh launch.

    Attempt numbers are reused across step visits and retries, so a previous
    visit's artifacts survive at the SAME keys. The reconciler reads the result
    key from its first poll — while the container is still running — and
    classifies whatever it found at exit; a stale object there replays the
    previous visit's verdict (run 07c4b440 recorded visit 1's payload
    byte-for-byte as visit 2's result). The container.log key is included
    because _capture_container_log skips capture when a non-empty object
    already exists (crash re-attach idempotency) — a stale one would suppress
    the new attempt's capture. Best-effort: a failure logs and continues.
    """
    if store is None:
        return
    keys = (
        result_key(run_id, step_id, attempt_no),
        progress_key(run_id, step_id, attempt_no),
        log_key(run_id, step_id, attempt_no, "agent"),
        log_key(run_id, step_id, attempt_no, "diagnostics"),
        log_key(run_id, step_id, attempt_no, "container"),
        # Session turn channels (ADR-016 §2): a cold-start at a reused attempt
        # must never inherit a stale inbox/outbox (usually a no-op delete).
        turn_inbox_key(run_id, step_id, attempt_no),
        turn_outbox_key(run_id, step_id, attempt_no),
    )
    for key in keys:
        try:
            await store.delete(key)
        except Exception as exc:  # noqa: BLE001 — best-effort hygiene
            logger.warning(
                "run %s: clearing stale attempt channel %s failed: %s",
                run_id, key, exc)


async def _prepare_launch_env(ctx, run, row, step_id, skill, attempt, deadline, *,
                              reviewer_feedback: str, handoff: dict | None,
                              store, session_mode: bool = False,
                              session_id: str | None = None,
                              session_resume: bool = False):
    """Build the launch env for one attempt: skill-bundle GET presign (the
    FROZEN pin on the step row — never the per-dispatch-resolved map, so mid-run
    skill edits can't change an in-flight step) + result/progress/log PUT
    presigns (ADR-014), plus the session channels + BB_SESSION identity for
    ad-hoc sessions (ADR-016 §2-3). Returns (env, context, reason) — a non-None
    reason means the launch must not proceed (classified by the caller)."""
    skill_key = row.skill_s3_key
    skill_sha = row.skill_sha256 or ""
    if not skill_key:
        # Only reachable for rows the init backfill couldn't stamp (pre-Phase-1
        # rows whose workflow changed) — fall back to the init-resolved bundle
        # and stamp the row (committed by the caller's launch transaction).
        bundle = ctx.skill_bundle.get(skill)
        if bundle is not None:
            skill_key, skill_sha = bundle
            row.skill_s3_key, row.skill_sha256 = bundle
        else:
            return None, None, f"step '{step_id}' has no skill bundle pin for '{skill}'"
    context = build_step_context(str(run.id), step_id, skill, run.story_id,
                                 ctx.workflow_spec, ctx.policy_spec,
                                 reviewer_feedback=reviewer_feedback, handoff=handoff,
                                 user_query=run.user_query or "")
    env = build_env_bundle(ctx, step_id=step_id, attempt_no=attempt,
                           skill=skill, model=ctx.model_map.get(step_id),
                           context=context)
    if session_mode:
        env["BB_SESSION"] = "1"
        if session_id:
            env["BB_SESSION_ID"] = session_id
        env["BB_SESSION_RESUME"] = "1" if session_resume else "0"
    if store is None:
        logger.warning(
            "run %s: no object store — launching step '%s' without BB_SKILL_URL "
            "or PUT URLs; engine cannot read its result/progress "
            "(tests/minimal deployments)", run.id, step_id)
        return env, context, None
    try:
        presigned = await store.presigned_get_url(skill_key, expires_in=900)
    except Exception as exc:  # noqa: BLE001 — any presign failure classifies as infra
        return None, None, f"presign failed for skill bundle {skill_key}: {exc}"
    env["BB_SKILL_URL"] = presigned.url
    env["BB_SKILL_SHA256"] = skill_sha
    try:
        env.update(await _launch_upload_contract(
            store, str(run.id), step_id, attempt, deadline))
        if session_mode:
            env.update(await _launch_turn_contract(
                store, str(run.id), step_id, attempt,
                session_id=session_id, resume=session_resume))
    except Exception as exc:  # noqa: BLE001 — any presign failure classifies as infra
        return None, None, f"presign PUT failed for step '{step_id}': {exc}"
    return env, context, None


async def _run_step(session: AsyncSession, ctx, config, runtime, step_id: str,
                    spec: dict, *, reviewer_feedback: str, handoff: dict | None,
                    publish=None,
                    cancel_event: asyncio.Event | None = None,
                    store=None) -> tuple[str, dict | None]:
    """Port of engine.py _run_step. Returns ("advanced"|"paused"|"failed"|
    "cancelled", outcome|None). Completion, routing, and any gate pause commit
    atomically — see the module docstring."""
    run = ctx.run
    skill = spec.get("skill", step_id)
    deadline = float(spec.get("deadline", 900))
    max_attempts = _env_int(ctx, "BB_MAX_ATTEMPTS", config.engine.max_attempts)

    row = await _get_step(session, run.id, step_id)
    if row is None:
        # Legacy run without step rows — stamp the init-resolved bundle pin
        # so the launch below always reads a frozen key.
        bundle = ctx.skill_bundle.get(skill)
        row = Step(run_id=run.id, step_id=step_id, skill=skill,
                   model_requested=ctx.model_map.get(step_id),
                   skill_s3_key=bundle[0] if bundle else None,
                   skill_sha256=bundle[1] if bundle else None)
        session.add(row)
        await session.flush()

    resuming = row.exec_state == ExecState.RUNNING
    start_attempt = max(1, row.attempt_no) if resuming else 1
    # Deadline math on re-attach must measure from the ORIGINAL launch — captured
    # before the attempt-start lines overwrite started_at.
    original_started = row.started_at.timestamp() if (resuming and row.started_at) else None

    for attempt in range(start_attempt, max_attempts + 1):
        row.exec_state = ExecState.RUNNING
        row.attempt_no = attempt
        row.started_at = datetime.now(timezone.utc)
        run.current_step = step_id
        record_transition(session, run.id, ExecState.PENDING, ExecState.RUNNING,
                          step_id=step_id, attempt_no=attempt,
                          reason=f"running skill {skill}")
        await session.commit()

        # Crash re-attach (ADR-003): a step left exec_state="running" with a
        # runtime handle resumes the SAME attempt — the container if it survived,
        # a fresh launch of the same attempt otherwise (push-lands-or-retry makes
        # the double-run safe: nothing counts until its push lands).
        h: Handle | None = None
        if resuming and row.fargate_task_arn:
            started = original_started or row.started_at.timestamp()
            h = await runtime.make_handle(str(run.id), step_id, attempt,
                                          row.fargate_task_arn, started)
            try:
                st = await runtime.status(h)
            except Exception:  # noqa: BLE001 — any status failure → treat as gone, relaunch
                st = {"state": "gone"}
            if st.get("state") != "gone":
                remaining = max(5.0, (started + deadline) - time.time())
                logger.info("re-attached container %s — %.1fs of deadline left",
                            h.container_id[:12], remaining)
                outcome = await reconcile(runtime, h, remaining, on_progress=(
                    _progress_publisher(publish, run.id, step_id, attempt)),
                    cancel_event=cancel_event, store=store)
            else:
                h = None
                record_transition(session, run.id, ExecState.RUNNING, ExecState.RUNNING,
                                  step_id=step_id, attempt_no=attempt,
                                  reason="container gone after crash — relaunching same attempt")
        if h is None:
            # Skill bundle + channel presigns: a fresh GET for the FROZEN pin on
            # the step row — never the per-dispatch-resolved map, so mid-run
            # skill edits can't change an in-flight step — plus the PUT URLs
            # for this attempt's deterministic keys (ADR-014). Pre-launch
            # failures mirror the transient exit-code path: retry the attempt,
            # then fail the run as failed_infra.
            env, context, reason = await _prepare_launch_env(
                ctx, run, row, step_id, skill, attempt, deadline,
                reviewer_feedback=reviewer_feedback, handoff=handoff, store=store)
            if env is None:
                logger.error("run %s: %s", run.id, reason)
                record_transition(session, run.id, ExecState.RUNNING,
                                  ExecState.RETRYING, step_id=step_id,
                                  attempt_no=attempt,
                                  result_status=Result.FAILED_INFRA, reason=reason)
                await session.commit()
                if attempt < max_attempts:
                    continue
                await _fail_run(session, run, reason=reason,
                                result_status=Result.FAILED_INFRA,
                                step_id=step_id, attempt_no=attempt)
                return ("failed", None)
            await _clear_attempt_channels(store, str(run.id), step_id, attempt)
            h = await runtime.launch(str(run.id), step_id, attempt, env, context=context)
            row.fargate_task_arn = h.container_id
            # Live-log reference rows (size 0 = "waiting for first upload") —
            # agent.log is heartbeated every ~5 s, so the platform can serve
            # and tail this attempt WHILE it runs, not only after it ends.
            # Commits atomically with the launch transition below.
            await register_logs_at_launch(session, run, step_id, attempt, store)
            record_transition(session, run.id, ExecState.RUNNING, ExecState.AWAITING_RESULT,
                              step_id=step_id, attempt_no=attempt,
                              reason="container launched")
            await session.commit()    # persist the handle BEFORE reconciling
            outcome = await reconcile(runtime, h, deadline, on_progress=(
                _progress_publisher(publish, run.id, step_id, attempt)),
                cancel_event=cancel_event, store=store)
        resuming = False

        st = outcome.get("status")

        # Capture container output while the container still exists — docker
        # logs die with it, and the cancel branch below stops it right after.
        # reconcile already captured on its own kill paths (cancel/timeout);
        # this covers every other terminal return. Idempotent by design
        # (head-check); no-op without a store.
        await _capture_container_log(store, runtime, h)

        # Stop-run: reconcile aborted on the cancel event (in-process), or the
        # DB shows the run cancelled (a cross-engine cancel handler wrote it
        # directly). Either way kill the container NOW — stop() ignores
        # keep_containers, unlike the cleanup below, so a live agent can never
        # push commits after the run was cancelled (push-lands-or-retry: the
        # branch reflects exactly completed steps).
        db_state = await session.scalar(select(Run.state).where(Run.id == run.id))
        if st == CANCELLED or db_state == "cancelled":
            await runtime.stop(h)
            row.exec_state = ExecState.FAILED
            row.result_status = CANCELLED
            row.ended_at = datetime.now(timezone.utc)
            # A cancelled attempt still spent money up to the kill — the
            # reconciler recovers what the log reported (cost_partial).
            cost = float(outcome.get("cost_usd") or 0)
            run.cost_usd = float(run.cost_usd or 0) + cost
            row.cost_usd = float(row.cost_usd or 0) + cost
            # The attempt's logs commit atomically with its terminal row —
            # even a cancelled attempt's partial log is worth keeping.
            await upload_step_logs(session, run, step_id, attempt, store)
            record_transition(session, run.id, ExecState.AWAITING_RESULT, ExecState.FAILED,
                              step_id=step_id, attempt_no=attempt, result_status=CANCELLED,
                              reason="run cancelled while this step was running — container stopped")
            await _cancel_run(session, run,
                              reason=f"cancelled while step '{step_id}' was running (stop requested)",
                              step_id=step_id, attempt_no=attempt, publish=publish)
            return ("cancelled", None)

        cost = float(outcome.get("cost_usd") or 0)
        run.cost_usd = float(run.cost_usd or 0) + cost
        row.cost_usd = float(row.cost_usd or 0) + cost

        # Log artifacts land in object storage with their reference rows in
        # the SAME transaction as the step's terminal transition below.
        await upload_step_logs(session, run, step_id, attempt, store)

        # Transparency (engine.py 795-816): out-of-vocabulary statuses and ignored
        # hints are recorded, never routed.
        allowed = set(ctx.workflow_spec.allowed_statuses(step_id))
        engine_statuses = TRANSIENT | {Result.FAILED_EXECUTION}
        if st not in allowed and st not in engine_statuses:
            record_transition(session, run.id, ExecState.AWAITING_RESULT,
                              ExecState.AWAITING_RESULT, step_id=step_id,
                              attempt_no=attempt, result_status=st,
                              reason="skill reported a status outside its vocabulary — recorded, not routed")
        hint = outcome.get("next_hint")
        if hint and ctx.workflow_spec.route_for(step_id, st) != "route_to":
            record_transition(session, run.id, ExecState.AWAITING_RESULT,
                              ExecState.AWAITING_RESULT, step_id=step_id,
                              attempt_no=attempt, result_status=st,
                              reason=f"next hint '{hint}' ignored — workflow has no route_to for this step")

        logs_text = ""
        if st not in (Result.COMPLETED, Result.BLOCK,
                      Result.CHANGES_REQUESTED, Result.ESCALATION_REQUIRED):
            logs_text = await runtime.logs(h)
        await runtime.cleanup(h)

        # Domain verdicts mean the skill ran and produced work — the row stays
        # completed (a gate, if any, hangs on the transition). The failed_*
        # family means nothing usable was produced — the row must read failed,
        # or the UI renders a dead step as "pending" and lists its artifacts.
        row.exec_state = (
            ExecState.COMPLETED
            if st in (Result.COMPLETED, Result.BLOCK,
                      Result.CHANGES_REQUESTED, Result.ESCALATION_REQUIRED)
            else ExecState.FAILED
        )
        row.result_status = st
        row.ended_at = datetime.now(timezone.utc)
        to_state = ExecState.COMPLETED if st == Result.COMPLETED else ExecState.FAILED
        record_transition(session, run.id, ExecState.AWAITING_RESULT, to_state,
                          step_id=step_id, attempt_no=attempt, result_status=st,
                          reason=outcome.get("reason"),
                          payload={"summary": outcome.get("summary"),
                                   "summary_full": outcome.get("summary_full"),
                                   "artifact": outcome.get("artifact"),
                                   "files": outcome.get("files") or [],
                                   "review_files": outcome.get("review_files") or [],
                                   "next_hint": outcome.get("next_hint"),
                                   "commit": outcome.get("commit"),
                                   "cost_usd": outcome.get("cost_usd"),
                                   "cost_reported": bool(outcome.get("cost_reported")),
                                   "cost_partial": bool(outcome.get("cost_partial"))})

        if st in TRANSIENT and attempt < max_attempts:
            record_transition(session, run.id, to_state, ExecState.RETRYING,
                              step_id=step_id, attempt_no=attempt,
                              reason=f"transient '{st}' — retrying in a fresh container")
            await session.commit()
            continue

        if st in TRANSIENT or st == Result.FAILED_EXECUTION:
            record_transition(session, run.id, to_state, ExecState.FAILED,
                              step_id=step_id, attempt_no=attempt, result_status=st,
                              reason="needs a human — not retrying automatically")
            reason = f"step '{step_id}' ended '{st}'"
            if outcome.get("reason"):
                reason += f": {outcome['reason']}"
            if logs_text:
                reason += f"\ncontainer logs:\n{logs_text[-4000:]}"
            await _fail_run(session, run, reason=reason, result_status=st,
                            step_id=step_id, attempt_no=attempt)
            return ("failed", None)

        # Domain verdict: route + gate, atomically with the completion.
        target = route_next(ctx.workflow_spec, step_id, st, outcome)
        gate = ctx.policy_spec.gate_for(step_id, st)
        if gate:
            run.state = "paused"
            record_transition(session, run.id, ExecState.COMPLETED,
                              ExecState.AWAITING_APPROVAL, step_id=step_id,
                              attempt_no=attempt, result_status=st,
                              reason="waiting for your approval",
                              payload=_gate_card(gate, outcome))
            await session.commit()
            await _publish(publish, {"type": "approval_required", "run_id": str(run.id),
                                     "step_id": step_id, "result_status": st})
            return ("paused", outcome)
        if target is None:
            await _fail_run(session, run, step_id=step_id, attempt_no=attempt,
                            result_status=st,
                            reason=f"workflow has no route for '{st}' from step '{step_id}'")
            return ("failed", None)
        if target != DONE and target not in ctx.workflow_spec.steps:
            await _fail_run(session, run, step_id=step_id, attempt_no=attempt,
                            result_status=st,
                            reason=f"workflow routed to unknown step '{target}'")
            return ("failed", None)
        run.current_step = target
        await session.commit()
        return ("advanced", outcome)

    # Defensive: attempt_no beyond max_attempts (manual DB edits) — don't strand the run.
    await _fail_run(session, run, step_id=step_id,
                    reason=f"step '{step_id}' attempt_no exceeds max_attempts ({max_attempts})")
    return ("failed", None)


def _progress_publisher(publish, run_id, step_id: str, attempt: int):
    async def on_progress(prog: dict) -> None:
        await _publish(publish, {"type": "step_progress", "run_id": str(run_id),
                                 "step_id": step_id, "attempt_no": attempt,
                                 "progress": prog})
    return on_progress


# ── Ad-hoc session turns (ADR-016 §2) ───────────────────────────────────
#
# A session run is paused at `awaiting_input` between turns (never at a gate —
# sessions have no gates). One dispatch = one turn = one pause. The step row
# stays exec_state="running" for the whole session: `attempt_no` numbers
# CONTAINER incarnations (cold-starts), and `turn_no` numbers turns across
# incarnations — the global-monotonic seq the inbox/outbox match on. The turn
# history is the Transition stream: each completed turn commits a row with
# {kind:"turn", seq, query, response, commit, files, cost} in its payload —
# durable + auditable independent of object storage.


async def _live_session_handle(runtime, run, row, step_id) -> Handle | None:
    """Re-attach the session's live container, or None when there is no live
    one (never launched / gone / already exited — the caller cold-starts a
    fresh incarnation). An exited container is cleaned up here: its last turn
    already committed before the outbox PUT, so removal is safe."""
    if row is None or not row.fargate_task_arn:
        return None
    started = row.started_at.timestamp() if row.started_at else time.time()
    h = await runtime.make_handle(str(run.id), step_id, row.attempt_no,
                                  row.fargate_task_arn, started)
    try:
        st = await runtime.status(h)
    except Exception:  # noqa: BLE001 — any status failure → treat as gone, cold-start
        st = {"state": "gone"}
    if st.get("state") == "running":
        return h
    if st.get("state") == "exited":
        await runtime.cleanup(h)
    return None


async def _write_turn_inbox(store, run_id: str, step_id: str, attempt_no: int,
                            payload: dict) -> None:
    """Write one turn (or the end sentinel) into the session inbox — one stable
    key, overwritten per turn, monotonic seq (ADR-016 §2). Best-effort: a failed
    write means the container answers nothing and the turn times out — a visible
    failure, never a silent hang. Log the key, never the payload (a query can
    contain anything)."""
    if store is None:
        return
    key = turn_inbox_key(run_id, step_id, attempt_no)
    try:
        await store.put(key, json.dumps(payload).encode("utf-8"),
                        content_type="application/json")
    except Exception:
        logger.warning("run %s: inbox write failed for %s", run_id, key,
                       exc_info=True)


async def _wait_container_exit(runtime, h: Handle | None, grace_s: float, *,
                               cancel_event: asyncio.Event | None = None,
                               alive_check=None) -> str:
    """Poll a session container until it exits (end/reap path). Returns
    "exited_0" | "exited_nonzero" | "gone" | "hung" | "cancelled" |
    "superseded". NEVER kills the container — the caller decides (graceful
    cleanup vs hard-kill fallback).

    ``alive_check`` (optional async callable → bool) is polled every tick: a
    False aborts the wait as "superseded" — the caller's claim was taken over
    (a turn dispatch flipped the session back to active) and the container is
    legitimately busy again. Without it a reaper could hard-kill a container
    mid-turn.
    """
    if h is None:
        return "gone"
    deadline = time.time() + grace_s
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if alive_check is not None and not await alive_check():
            return "superseded"
        try:
            st = await runtime.status(h)
        except Exception:  # noqa: BLE001 — status failure reads as gone
            return "gone"
        if st.get("state") == "exited":
            return "exited_0" if st.get("exit_code") == 0 else "exited_nonzero"
        if st.get("state") == "gone":
            return "gone"
        if time.time() > deadline:
            return "hung"
        await asyncio.sleep(POLL_INTERVAL)


async def _finish_turn(session: AsyncSession, run, row, step_id: str, seq: int,
                       query: str, outcome: dict, publish) -> None:
    """Record a successful turn and pause the run at awaiting_input. The step
    row stays running — the live container is the session."""
    payload = {"kind": "turn", "seq": seq, "query": query,
               "response": outcome.get("response", ""),
               "commit": outcome.get("commit"),
               "files": outcome.get("files") or [],
               "cost_usd": outcome.get("cost_usd"),
               "cost_reported": bool(outcome.get("cost_reported")),
               "cost_partial": bool(outcome.get("cost_partial"))}
    run.state = "paused"
    run.session_phase = "active"
    run.session_last_activity_at = datetime.now(timezone.utc)
    record_transition(session, run.id, ExecState.RUNNING, ExecState.AWAITING_INPUT,
                      step_id=step_id, attempt_no=row.attempt_no,
                      reason=f"turn {seq} answered — awaiting input",
                      payload=payload)
    await session.commit()
    await _publish(publish, {"type": "turn_completed", "run_id": str(run.id),
                             "step_id": step_id, "seq": seq})


async def _finish_failed_turn(session: AsyncSession, run, row, step_id: str,
                              seq: int, query: str, status: str, reason: str,
                              cost: float, cost_reported: bool, publish) -> None:
    """Record a failed turn and pause the run at awaiting_input — the session
    survives; the next turn cold-starts a fresh incarnation, and End stays
    available. The container is already dead on every failed path."""
    payload = {"kind": "turn", "seq": seq, "query": query,
               "result_status": status, "response": reason,
               "cost_usd": cost, "cost_reported": cost_reported}
    run.state = "paused"
    run.session_phase = "active"
    run.session_last_activity_at = datetime.now(timezone.utc)
    record_transition(session, run.id, ExecState.RUNNING, ExecState.AWAITING_INPUT,
                      step_id=step_id, attempt_no=row.attempt_no,
                      reason=f"turn {seq} failed ({status}) — awaiting input",
                      payload=payload)
    await session.commit()
    await _publish(publish, {"type": "turn_failed", "run_id": str(run.id),
                             "step_id": step_id, "seq": seq,
                             "result_status": status})


async def _session_turn(session: AsyncSession, ctx, config, runtime, *, query: str,
                        publish=None, cancel_event: asyncio.Event | None = None,
                        store=None) -> None:
    """Drive ONE ad-hoc session turn (ADR-016 §2). The run is `running` while
    the turn is in flight, `paused` (awaiting_input) between turns.

    Ordering invariant: the next turn seq commits BEFORE the inbox write — a
    crash between the two re-delivers the ORIGINAL turn item (stale-claim
    reaper, query in its payload), which re-enters here and increments again.
    Seq collisions are impossible; skipped seqs are harmless (monotonic).

    Container resolution: re-attach the live container when it survived
    (attempt_no unchanged); a gone/exited container cold-starts a FRESH
    incarnation (attempt_no + 1, --resume <session-id> — ADR-016 §3)."""
    run = ctx.run
    step_id = run.current_step or ctx.workflow_spec.start
    spec = ctx.workflow_spec.steps.get(step_id)
    if spec is None:
        await _fail_run(session, run, reason=f"ad-hoc session routed to unknown step '{step_id}'")
        return
    skill = spec.get("skill", step_id)
    deadline = float(spec.get("deadline", 900))
    row = await _get_step(session, run.id, step_id)
    if row is None:
        await _fail_run(session, run, step_id=step_id,
                        reason="ad-hoc session step row missing")
        return
    query = (query or "").strip()
    if not query:
        await _fail_run(session, run, step_id=step_id,
                        reason="session turn dispatched with an empty query")
        return

    # 1. Commit the next seq + flip to running (atomically with the dispatch
    # transition) BEFORE the inbox write — see the invariant above.
    seq = row.turn_no + 1
    row.turn_no = seq
    prev = run.state
    run.state = "running"
    run.session_phase = "active"
    run.session_last_activity_at = datetime.now(timezone.utc)
    run.current_step = step_id
    record_transition(session, run.id, prev, ExecState.RUNNING,
                      step_id=step_id, attempt_no=row.attempt_no,
                      reason=f"turn {seq} dispatched",
                      # kind "turn_request", not "turn" — the chat transcript
                      # builder keys on kind=="turn" completion rows; the
                      # request rows are the dispatch audit trail.
                      payload={"kind": "turn_request", "seq": seq, "query": query})
    await session.commit()

    # 2. Resolve the container: re-attach the live one or cold-start a fresh
    # incarnation (attempt_no = container incarnation, not a retry counter).
    h = await _live_session_handle(runtime, run, row, step_id)
    if h is None:
        # Cold-start. The FIRST launch keeps the row's fresh attempt_no (1) —
        # exec_state stays RUNNING across incarnations, so PENDING means
        # "never launched"; every later cold-start (reaped / gone / exited
        # container) increments it.
        attempt = (row.attempt_no + 1 if row.exec_state == ExecState.RUNNING
                   else max(1, row.attempt_no))
        row.attempt_no = attempt
        row.exec_state = ExecState.RUNNING
        row.started_at = datetime.now(timezone.utc)
        row.fargate_task_arn = None
        session_id = run.claude_session_id
        if not session_id:
            # Legacy row predating ADR-016 §3 (minted at init now) — mint on
            # first launch so the transcript contract still holds.
            session_id = run.claude_session_id = str(uuid.uuid4())
        env, context, reason = await _prepare_launch_env(
            ctx, run, row, step_id, skill, attempt, deadline,
            reviewer_feedback="", handoff=None, store=store, session_mode=True,
            session_id=session_id, session_resume=attempt > 1)
        if env is None:
            logger.error("run %s: %s", run.id, reason)
            await _finish_failed_turn(session, run, row, step_id, seq, query,
                                      Result.FAILED_INFRA, reason, 0, False, publish)
            return
        try:
            await _clear_attempt_channels(store, str(run.id), step_id, attempt)
            h = await runtime.launch(str(run.id), step_id, attempt, env, context=context)
        except Exception:
            logger.exception("run %s: session cold-start launch failed", run.id)
            await _finish_failed_turn(session, run, row, step_id, seq, query,
                                      Result.FAILED_INFRA,
                                      "session container launch failed", 0, False, publish)
            return
        row.fargate_task_arn = h.container_id
        await register_logs_at_launch(session, run, step_id, attempt, store)
        record_transition(session, run.id, ExecState.RUNNING, ExecState.RUNNING,
                          step_id=step_id, attempt_no=attempt,
                          reason="session container launched (cold-start)")
        await session.commit()    # persist the handle BEFORE reconciling

    # 3. Write the turn into the inbox (stable key, monotonic seq) and
    # reconcile the outbox reply.
    await _write_turn_inbox(store, str(run.id), step_id, row.attempt_no,
                            {"seq": seq, "kind": "turn", "query": query})
    outcome = await reconcile_turn(runtime, h, seq, deadline,
                                   turn_started_at=time.time(),
                                   cancel_event=cancel_event, store=store)
    st = outcome.get("status")

    # Capture container output while the container still exists (the cancel
    # branch stops it right after).
    await _capture_container_log(store, runtime, h)

    db_state = await session.scalar(select(Run.state).where(Run.id == run.id))
    if st == CANCELLED or db_state == "cancelled":
        await runtime.stop(h)
        row.exec_state = ExecState.FAILED
        row.result_status = CANCELLED
        row.ended_at = datetime.now(timezone.utc)
        row.fargate_task_arn = None
        cost = float(outcome.get("cost_usd") or 0)
        run.cost_usd = float(run.cost_usd or 0) + cost
        row.cost_usd = float(row.cost_usd or 0) + cost
        await upload_step_logs(session, run, step_id, row.attempt_no, store)
        record_transition(session, run.id, ExecState.RUNNING, ExecState.FAILED,
                          step_id=step_id, attempt_no=row.attempt_no,
                          result_status=CANCELLED,
                          reason="run cancelled while the session turn was in flight — container stopped")
        await _cancel_run(session, run,
                          reason="cancelled during session turn (stop requested)",
                          step_id=step_id, attempt_no=row.attempt_no, publish=publish)
        return

    cost = float(outcome.get("cost_usd") or 0)
    run.cost_usd = float(run.cost_usd or 0) + cost
    row.cost_usd = float(row.cost_usd or 0) + cost

    if st == Result.COMPLETED:
        await _finish_turn(session, run, row, step_id, seq, query, outcome, publish)
        return

    # Failed turn: the container is dead on every non-completed path (reconcile
    # cleans up on timeout; the others died on their own) — tidy up so the next
    # turn cold-starts.
    await runtime.cleanup(h)
    row.fargate_task_arn = None
    await _finish_failed_turn(session, run, row, step_id, seq, query, st,
                              outcome.get("reason") or st, cost,
                              bool(outcome.get("cost_reported")), publish)


async def _end_session(session: AsyncSession, ctx, config, runtime, *, publish=None,
                       cancel_event: asyncio.Event | None = None,
                       store=None) -> None:
    """Explicit End session (ADR-016 §2): write the end sentinel, wait for the
    container to commit+push+exit on its own — never docker rm -f here: the
    exit is the moment the final commit lands, and R1 (unpushed work dies with
    the container) rules out a kill. A hung container gets the hard-kill
    fallback after BB_ADHOC_END_GRACE_SECONDS — by then every turn has already
    committed per turn, so only the transcript upload is lost (Phase 3).

    exit 0 (or no live container to signal — a prior reap/exit already
    committed) → run completed, session_phase ended. Anything else keeps the
    session open: paused at awaiting_input with a note, so the user can end
    again or send another turn (cold-start)."""
    run = ctx.run
    step_id = run.current_step or ctx.workflow_spec.start
    row = await _get_step(session, run.id, step_id)
    if row is None:
        await _fail_run(session, run, step_id=step_id,
                        reason="ad-hoc session step row missing at end")
        return
    grace = float(config.engine.ad_hoc_end_grace_seconds)

    # End sentinel: a NEW seq so the container detects it against its last seen
    # turn. Committed before the write (same invariant as turns). The seq is
    # DERIVED from turn_no but never persisted — turn_no numbers turns, and a
    # sentinel that never completes as a turn must not consume a turn number
    # (the next real turn takes this seq: it either ends the session here, or
    # is superseded by that turn, never both).
    seq = row.turn_no + 1
    run.state = "running"
    run.session_last_activity_at = datetime.now(timezone.utc)
    record_transition(session, run.id, ExecState.AWAITING_INPUT, ExecState.RUNNING,
                      step_id=step_id, attempt_no=row.attempt_no,
                      reason="end session requested",
                      payload={"kind": "session_end_request", "seq": seq})
    await session.commit()

    h = await _live_session_handle(runtime, run, row, step_id)
    if h is not None:
        await _write_turn_inbox(store, str(run.id), step_id, row.attempt_no,
                                {"seq": seq, "kind": "end"})
        wait = await _wait_container_exit(runtime, h, grace,
                                          cancel_event=cancel_event)
    else:
        # No live container — a prior reap/exit already committed the session's
        # work. Ending is pure bookkeeping.
        wait = "gone"

    db_state = await session.scalar(select(Run.state).where(Run.id == run.id))
    if wait == "cancelled" or db_state == "cancelled":
        return    # cancel handler owns the terminal state + container
    if wait == "hung" and h is not None:
        await runtime.stop(h)
        logger.warning("run %s: session container hung at end — force-removed", run.id)
    elif h is not None:
        await runtime.cleanup(h)    # exited (graceful or not); keep_containers respected
    row.fargate_task_arn = None
    row.ended_at = datetime.now(timezone.utc)
    run.session_phase = "ended"
    run.session_last_activity_at = datetime.now(timezone.utc)
    await upload_step_logs(session, run, step_id, row.attempt_no, store)

    if wait in ("exited_0", "gone"):
        row.exec_state = ExecState.COMPLETED
        row.result_status = Result.COMPLETED
        run.state = "completed"
        record_transition(session, run.id, ExecState.RUNNING, "completed",
                          step_id=step_id, attempt_no=row.attempt_no,
                          result_status=Result.COMPLETED,
                          reason="session ended (user request)",
                          payload={"kind": "session_end", "seq": seq})
        await session.commit()
        await _publish(publish, {"type": "run_completed", "run_id": str(run.id)})
        return

    # End attempt failed (non-zero exit / hung): keep the session open — never
    # lose a session to a failed shutdown.
    run.state = "paused"
    record_transition(session, run.id, ExecState.RUNNING, ExecState.AWAITING_INPUT,
                      step_id=step_id, attempt_no=row.attempt_no,
                      reason=f"end session failed ({wait}) — session still open",
                      payload={"kind": "session_end_failed", "seq": seq})
    await session.commit()
    await _publish(publish, {"type": "turn_failed", "run_id": str(run.id),
                             "step_id": step_id, "seq": seq,
                             "result_status": Result.FAILED_INFRA})


async def reap_idle_session(config, runtime, *, sessionmaker, run_id,
                            publish=None, store=None) -> None:
    """Reap one idle ad-hoc session (ADR-016 §2) — the worker's per-run task.

    The worker flipped session_phase to 'reaping' atomically (double-fire
    guard) and bumped session_last_activity_at is done HERE first — a live
    reap never looks stale, a crashed one becomes re-eligible after the idle
    threshold. The flow mirrors _end_session's graceful path (sentinel →
    wait → never rm -f → hard-kill fallback), but the run stays PAUSED: the
    session ends (phase 'ended', handle cleared) while the run keeps accepting
    turns — the next one cold-starts a fresh incarnation (Phase 3 resumes the
    transcript)."""
    async with sessionmaker() as session:
        run = await session.get(Run, run_id)
        if run is None or run.run_kind != "adhoc" or run.session_phase != "reaping":
            return    # reaped by another task / no longer eligible
        step_id = run.current_step or ""
        row = await _get_step(session, run.id, step_id) if step_id else None
        if row is None:
            run.session_phase = "ended"
            await session.commit()
            return
        grace = float(config.engine.ad_hoc_end_grace_seconds)

        # Sentinel seq derived but not persisted (same contract as
        # _end_session): turn_no numbers turns, not sentinels.
        seq = row.turn_no + 1
        run.session_last_activity_at = datetime.now(timezone.utc)
        record_transition(session, run.id, ExecState.AWAITING_INPUT,
                          ExecState.AWAITING_INPUT, step_id=step_id,
                          attempt_no=row.attempt_no,
                          reason="session reaped after idle — ending gracefully",
                          payload={"kind": "session_reap", "seq": seq})
        await session.commit()

        h = await _live_session_handle(runtime, run, row, step_id)
        wait = "gone"
        if h is not None:
            await _write_turn_inbox(store, str(run.id), step_id, row.attempt_no,
                                    {"seq": seq, "kind": "end"})

            async def _still_reaping() -> bool:
                # Column-only scalar select — bypasses the session identity map,
                # so this reads the COMMITTED phase (a turn dispatch flips it to
                # 'active' without this task's cached object seeing it).
                phase = await session.scalar(
                    select(Run.session_phase).where(Run.id == run_id))
                return phase == "reaping"

            wait = await _wait_container_exit(runtime, h, grace,
                                              alive_check=_still_reaping)
            if wait == "superseded":
                logger.info("run %s: reap superseded — a turn took the session "
                            "back over; container stays live", run_id)
            elif wait == "hung":
                await runtime.stop(h)
                logger.warning("run %s: session container hung at reap — force-removed",
                               run_id)
            elif wait != "gone":
                await runtime.cleanup(h)

        # Finalize only if the session is still being reaped — a turn dispatch
        # flips the phase back to 'active' and owns the handle/container now
        # (rowcount 0 = taken over). The conditional write keeps this task from
        # clobbering a live session's phase or handle.
        flipped = await session.execute(
            update(Run)
            .where(Run.id == run_id, Run.session_phase == "reaping")
            .values(session_phase="ended",
                    session_last_activity_at=datetime.now(timezone.utc)))
        if flipped.rowcount:
            row.fargate_task_arn = None
            await upload_step_logs(session, run, step_id, row.attempt_no, store)
        await session.commit()
        await _publish(publish, {"type": "session_ended", "run_id": str(run_id),
                                 "reason": "idle"})


# ── Gate decisions ──────────────────────────────────────────────────────

async def _apply_approve(session: AsyncSession, ctx, payload: dict):
    """Reviewer approves the gated step. Routes via the on: map — `route_to`
    steps honour the next_hint captured in the gate card. Returns
    (target, reviewer_feedback, handoff) or ("", …) when the run ended."""
    run = ctx.run
    step_id = run.current_step or ""
    gate_row = await _last_gate_transition(session, run.id)
    comment = str(payload.get("comment") or "")
    actor = str(payload.get("actor") or "reviewer")

    status: str | None = None
    card: dict = {}
    if gate_row is not None:
        status = gate_row.result_status or (gate_row.payload or {}).get("result_status")
        card = gate_row.payload or {}
    if status is None:
        row = await _get_step(session, run.id, step_id)
        status = row.result_status if row else None
    if status is None:
        await _fail_run(session, run, reason="cannot approve: no gate outcome recorded for this run")
        return "", "", None

    record_transition(session, run.id, ExecState.AWAITING_APPROVAL,
                      ExecState.COMPLETED, step_id=step_id,
                      attempt_no=gate_row.attempt_no if gate_row else RUN_LEVEL_ATTEMPT,
                      actor=actor, result_status=status,
                      reason="reviewer chose: approve" + (f" — {comment}" if comment else ""))

    outcome = {"status": status, "next_hint": card.get("next_hint"),
               "summary": card.get("summary"), "files": card.get("files") or [],
               "review_files": card.get("review_files") or []}
    target = route_next(ctx.workflow_spec, step_id, status, outcome)
    if target is None or (target != DONE and target not in ctx.workflow_spec.steps):
        await _fail_run(session, run, step_id=step_id,
                        result_status=status,
                        reason=f"workflow has no route for '{status}' from step '{step_id}' after approval")
        return "", "", None

    run.state = "running"
    run.current_step = target
    await session.commit()
    handoff = _handoff_for(outcome, step_id, status) if status != Result.COMPLETED else None
    return target, comment, handoff


async def _apply_send_back(session: AsyncSession, ctx, payload: dict):
    """Reviewer sends the run back (ADR-007): reset every later step to pending,
    `current_step = target`, reviewer comment becomes the next run's
    reviewer_feedback. Returns (target, feedback) or ("", "") on failure."""
    run = ctx.run
    target = str(payload.get("send_back_to") or "").strip()
    comment = str(payload.get("comment") or "")
    actor = str(payload.get("actor") or "reviewer")

    if target not in ctx.workflow_spec.steps:
        # The platform validates, but a stale/foreign decision must not corrupt the run.
        await _fail_run(session, run,
                        reason=f"send_back target '{target or '(none)'}' is not a workflow step")
        return "", ""

    step_id = run.current_step or ""
    gate_row = await _last_gate_transition(session, run.id)

    # Reset set: everything after the target, plus the gated step itself when the
    # reviewer sends it back to redo its own work.
    reset_ids = steps_after(ctx.workflow_spec, target)
    if target == step_id:
        reset_ids.append(target)
    for sid in reset_ids:
        row = await _get_step(session, run.id, sid)
        if row is None or row.exec_state == ExecState.PENDING:
            continue
        prev = row.exec_state
        row.exec_state = ExecState.PENDING
        row.result_status = None
        row.attempt_no = 1
        row.fargate_task_arn = None
        row.started_at = None
        row.ended_at = None
        record_transition(session, run.id, prev, ExecState.PENDING, step_id=sid,
                          attempt_no=1, actor=actor,
                          reason=f"send_back: reset (reviewer sent run back to '{target}')")

    record_transition(session, run.id, ExecState.AWAITING_APPROVAL,
                      ExecState.COMPLETED, step_id=step_id,
                      attempt_no=gate_row.attempt_no if gate_row else RUN_LEVEL_ATTEMPT,
                      actor=actor,
                      reason="reviewer chose: send back" +
                             (f" to {target} — {comment}" if comment else f" to {target}"))

    run.state = "running"
    run.current_step = target
    await session.commit()
    return target, comment


async def _renotify_gate(session: AsyncSession, ctx, publish) -> None:
    """A `resume` (or stale `start`) hit a still-open gate — no state change,
    just re-emit the approval event from the persisted gate card."""
    run = ctx.run
    gate_row = await _last_gate_transition(session, run.id)
    card = (gate_row.payload or {}) if gate_row else {}
    await _publish(publish, {"type": "approval_required", "run_id": str(run.id),
                             "step_id": run.current_step or "",
                             "result_status": card.get("result_status"),
                             "renotified": True})
