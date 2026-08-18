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
import logging
import time
from datetime import datetime, timezone

from bheembhai.models.run import Run, Step, Transition
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from engine_service.contexts import build_env_bundle, build_step_context
from engine_service.log_upload import upload_step_logs
from engine_service.persistence import (
    RUN_LEVEL_ATTEMPT,
    RUN_LEVEL_STEP,
    record_transition,
)
from engine_service.run_init import init_run
from engine_service.runtime import CANCELLED, Handle, _dump_container_log, reconcile
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
    ctx = await init_run(session, run.id, config, secure_storage)

    start: str | None = None
    reviewer_feedback = ""
    handoff: dict | None = None

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
        cap = max(1, config.engine.max_step_visits)
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
    max_attempts = max(1, config.engine.max_attempts)

    row = await _get_step(session, run.id, step_id)
    if row is None:
        row = Step(run_id=run.id, step_id=step_id, skill=skill,
                   model_requested=ctx.model_map.get(step_id))
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

        context = build_step_context(str(run.id), step_id, skill, run.story_id,
                                     ctx.workflow_spec, ctx.policy_spec,
                                     reviewer_feedback=reviewer_feedback, handoff=handoff)
        env = build_env_bundle(ctx, step_id=step_id, attempt_no=attempt,
                               skill=skill, model=ctx.model_map.get(step_id),
                               context=context)

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
                    cancel_event=cancel_event)
            else:
                h = None
                record_transition(session, run.id, ExecState.RUNNING, ExecState.RUNNING,
                                  step_id=step_id, attempt_no=attempt,
                                  reason="container gone after crash — relaunching same attempt")
        if h is None:
            h = await runtime.launch(str(run.id), step_id, attempt, env, context=context)
            row.fargate_task_arn = h.container_id
            record_transition(session, run.id, ExecState.RUNNING, ExecState.AWAITING_RESULT,
                              step_id=step_id, attempt_no=attempt,
                              reason="container launched")
            await session.commit()    # persist the handle BEFORE reconciling
            outcome = await reconcile(runtime, h, deadline, on_progress=(
                _progress_publisher(publish, run.id, step_id, attempt)),
                cancel_event=cancel_event)
        resuming = False

        st = outcome.get("status")

        # Capture container output while the container still exists — docker
        # logs die with it, and the cancel branch below stops it right after.
        # reconcile already captured on its own kill paths (cancel/timeout);
        # this covers every other terminal return. Idempotent by design.
        await _dump_container_log(runtime, h)

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
            await upload_step_logs(session, run, step_id, attempt, h, store)
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
        await upload_step_logs(session, run, step_id, attempt, h, store)

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
