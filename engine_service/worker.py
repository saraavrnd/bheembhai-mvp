"""Worker loop — claims work from work_queue via SKIP LOCKED and dispatches (ADR-003).

Each work item is a dispatch token: the dispatch task advances the run state
machine to its next pause (gate or terminal) and the item then goes `done`.
Claiming is cheap and non-blocking; the SKIP LOCKED loop never waits on a
multi-minute run.

Guards against double-driving a run:
  - per-run asyncio locks (bounded by distinct runs, never popped);
  - claim re-assert (UPDATE … WHERE claimed_by=me) inside the lock — a recovery
    demotion or another engine's supersede voids the dispatch before it runs;
  - supersede: sibling claimed items for the same run are demoted claimed→pending
    (NOT done — a pending gate decision must survive) so exactly one dispatch is
    live per run.

`claimed_by` identifies a PROCESS (engine_id + per-boot suffix), not an engine:
after a restart the new process must never adopt the dead one's claims — its
heartbeat loop would otherwise keep them permanently fresh, recovery would never
see them stale, and their runs would freeze in `running` forever. A stale-claim
reaper in the loop demotes claims whose heartbeat expired (the claiming process
died); the re-claim resumes the run from persisted state (ADR-003).
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.config import AppConfig
from bheembhai.database import get_sessionmaker
from bheembhai.models.run import Run
from bheembhai.models.work_queue import WorkQueueItem

from engine_service.metrics import METRICS
from engine_service.persistence import record_transition
from engine_service.run_init import InitFailure
from engine_service.state_machine import TERMINAL_STATES, _last_gate_transition, drive_run
from engine_service.workflow import ExecState, Result

logger = logging.getLogger(__name__)

# Configured once at startup (engine_service.main) — the runtime, SecureStorage,
# and ObjectStorage backend the dispatches; the optional publish callback fires
# engine→platform events.
_runtime = None
_secure_storage = None
_publish = None
_store = None

_run_locks: dict[uuid.UUID, asyncio.Lock] = {}
_dispatches: set[asyncio.Task] = set()
# Live dispatch registry (stop-run): run_id -> {"event": cancel signal, "task": the
# dispatch task}. Registered synchronously inside _process_item — before the worker
# loop can claim the NEXT item — so a `cancel` item always finds the dispatch it
# must signal (the worker loop is sequential: no other item is processed between
# create_task and this registration).
_active: dict[uuid.UUID, dict] = {}
_process_claim_id: str | None = None


async def _publish_event(event: dict) -> None:
    """Engine→platform event — non-fatal on failure."""
    if _publish is None:
        return
    try:
        await _publish(event)
    except Exception:
        logger.exception("publish failed (non-fatal)")


def _claim_identity(config: AppConfig) -> str:
    """This process's claim signature — engine_id + a per-boot random suffix.

    Memoized for the process lifetime. Two restarts of the same engine must not
    share an identity, or the new process's heartbeat loop would keep the dead
    process's claims fresh forever (see module docstring).
    """
    global _process_claim_id
    if _process_claim_id is None:
        _process_claim_id = f"{config.engine.engine_id}:{uuid.uuid4().hex[:12]}"
    return _process_claim_id


def configure_worker(*, runtime, secure_storage, publish=None, store=None) -> None:
    """Wire the runtime + SecureStorage + ObjectStorage into dispatch tasks
    (called from lifespan)."""
    global _runtime, _secure_storage, _publish, _store
    _runtime = runtime
    _secure_storage = secure_storage
    _publish = publish
    _store = store


def _lock_for(run_id: uuid.UUID) -> asyncio.Lock:
    lock = _run_locks.get(run_id)
    if lock is None:
        lock = _run_locks[run_id] = asyncio.Lock()
    return lock


async def worker_loop(config: AppConfig) -> None:
    """Continuously poll work_queue for pending items, claim and dispatch them.

    Runs as a background asyncio task for the lifetime of the Engine service.
    Each Engine instance independently polls; SKIP LOCKED ensures no two engines
    claim the same work item. A companion heartbeat task keeps claimed items alive
    while their dispatch runs (possibly minutes).
    """
    heartbeat_task = asyncio.create_task(_heartbeat_loop(config))

    try:
        while True:
            sessionmaker = get_sessionmaker()
            if sessionmaker is None:
                await asyncio.sleep(1)
                continue

            try:
                async with sessionmaker() as session:
                    await _reap_stale_claims(session, config)
                    claimed = await _claim_next_item(session, config)
                    if claimed:
                        await _process_item(session, claimed, config)
            except Exception:
                logger.exception("Worker loop iteration failed — retrying")

            await asyncio.sleep(config.engine.poll_interval_seconds)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _reap_stale_claims(session: AsyncSession, config: AppConfig) -> int:
    """Demote `claimed` items whose heartbeat expired back to `pending`.

    A claim is only kept fresh by the claiming process's own heartbeat loop, so
    an expired heartbeat means that process is dead (or its loop wedged past the
    threshold) — safe to re-queue; the re-claim resumes the run from persisted
    state. Runs each worker-loop iteration: one cheap UPDATE, committed here so
    it lands even when no item is claimed afterwards.
    """
    stale_since = datetime.now(timezone.utc) - timedelta(
        seconds=config.engine.stale_heartbeat_threshold_seconds)
    result = await session.execute(
        update(WorkQueueItem)
        .where(WorkQueueItem.state == "claimed",
               WorkQueueItem.heartbeat_at.is_not(None),
               WorkQueueItem.heartbeat_at < stale_since)
        .values(state="pending", claimed_by=None, claimed_at=None,
                heartbeat_at=None)
        .returning(WorkQueueItem.id))
    await session.commit()
    stale = result.scalars().all()
    for item_id in stale:
        logger.warning("reaped stale claim item id=%s (claiming process dead) — "
                       "re-queued for dispatch", item_id)
    METRICS.orphaned_items = max(0, METRICS.orphaned_items - len(stale))
    return len(stale)


async def _claim_next_item(
    session: AsyncSession, config: AppConfig
) -> WorkQueueItem | None:
    """Claim the oldest pending work item using SELECT ... FOR UPDATE SKIP LOCKED."""
    stmt = (
        select(WorkQueueItem)
        .where(WorkQueueItem.state == "pending")
        .order_by(WorkQueueItem.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()

    if item is None:
        METRICS.queue_depth = 0
        return None

    now = datetime.now(timezone.utc)
    item.state = "claimed"
    item.claimed_by = _claim_identity(config)
    item.claimed_at = now
    item.heartbeat_at = now
    await session.commit()
    await session.refresh(item)

    depth = await session.execute(
        select(func.count()).select_from(WorkQueueItem).where(WorkQueueItem.state == "pending"))
    METRICS.queue_depth = depth.scalar_one()

    logger.info(
        "Claimed work item id=%s run_id=%s action=%s",
        item.id, item.run_id, item.action,
    )
    return item


async def _process_item(
    session: AsyncSession, item: WorkQueueItem, config: AppConfig
) -> None:
    """Spawn a dispatch task for the claimed item and return immediately.

    The dispatch owns its session; this claim session only launched it."""
    if _runtime is None or _secure_storage is None:
        raise RuntimeError("worker not configured — call configure_worker() first")
    if item.action == "cancel":
        # Stop-run token: handled WITHOUT the per-run lock — it must never
        # queue behind the run's live dispatch (a step can run for minutes).
        task = asyncio.create_task(_cancel_guarded(config, item.id))
        _dispatches.add(task)
        task.add_done_callback(_dispatches.discard)
        METRICS.active_dispatches = len(_dispatches)
        return
    # start / continue: register the dispatch + its cancel event in _active
    # synchronously here, before the worker loop can claim the next item.
    cancel_event = asyncio.Event()
    task = asyncio.create_task(
        _dispatch_guarded(config, item.id, cancel_event=cancel_event))
    _dispatches.add(task)
    _active[item.run_id] = {"event": cancel_event, "task": task}
    task.add_done_callback(_make_done_callback(item.run_id, task))
    METRICS.active_dispatches = len(_dispatches)


def _make_done_callback(run_id: uuid.UUID, task: asyncio.Task):
    """Done-callback: drop the task from _dispatches and, if it is still the
    registered dispatch for its run, clear the _active entry (a newer dispatch
    for the same run keeps its own entry)."""
    def _done(_t) -> None:
        _dispatches.discard(task)
        active = _active.get(run_id)
        if active is not None and active["task"] is task:
            del _active[run_id]
        METRICS.active_dispatches = len(_dispatches)
    return _done


async def _dispatch_guarded(config: AppConfig, item_id, *,
                            cancel_event: asyncio.Event | None = None) -> None:
    """One work item, end to end: re-assert the claim under the per-run lock,
    supersede siblings, drive the state machine, mark the item done."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        item = await session.get(WorkQueueItem, item_id)
        if item is None:
            return

        async with _lock_for(item.run_id):
            # Supersede sibling claimed items for this run (demote, never done —
            # a pending decision must survive), then re-assert OUR claim. If the
            # re-assert touches 0 rows, another engine/recovery owns this item.
            await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.run_id == item.run_id,
                       WorkQueueItem.state == "claimed",
                       WorkQueueItem.id != item.id)
                .values(state="pending", claimed_by=None, claimed_at=None,
                        heartbeat_at=None))
            reassert = await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.id == item.id,
                       WorkQueueItem.state == "claimed",
                       WorkQueueItem.claimed_by == _claim_identity(config))
                .values(heartbeat_at=datetime.now(timezone.utc)))
            await session.commit()
            if reassert.rowcount == 0:
                logger.warning("claim lost for item %s — another engine/recovery owns it; aborting dispatch",
                               item_id)
                return

            try:
                await drive_run(session, item, config, _runtime, _secure_storage,
                                publish=_publish, cancel_event=cancel_event,
                                store=_store)
            except InitFailure as exc:
                logger.error("run %s init failed (%s): %s", item.run_id, exc.kind, exc.reason)
                run = await session.get(Run, item.run_id)
                if run is not None and run.state not in TERMINAL_STATES:
                    prev = run.state
                    run.state = "failed"
                    record_transition(session, run.id, prev, "failed",
                                      result_status=exc.kind, reason=exc.reason)
            except Exception:
                logger.exception("dispatch crashed for item %s — run failed, needs a human",
                                 item_id)
                run = await session.get(Run, item.run_id)
                if run is not None and run.state not in TERMINAL_STATES:
                    prev = run.state
                    run.state = "failed"
                    record_transition(session, run.id, prev, "failed",
                                      result_status=Result.FAILED_INFRA,
                                      reason="engine error mid-dispatch — run halted, needs a human")
            finally:
                item.state = "done"
                await session.commit()
                METRICS.active_dispatches = len(_dispatches)


async def _cancel_guarded(config: AppConfig, item_id) -> None:
    """Handle a `cancel` work item (stop-run) — WITHOUT the per-run lock.

    Taking the per-run lock here would deadlock: the run's live dispatch holds
    it for the duration of a step (minutes). Instead the handler signals the
    dispatch's in-memory cancel event and waits for the dispatch to reach a
    checkpoint and record the terminal state itself (sub-second — the reconciler
    checks the event every POLL_INTERVAL). Only if there is no live dispatch
    (paused at a gate / never started) or the dispatch does not finish in
    cancel_wait_seconds does the handler record the terminal state directly.

    A `cancel` arriving from another ENGINE process has no in-memory event to
    signal — there the direct DB write is the whole mechanism, and the remote
    dispatch observes it (the state machine re-checks runs.state at every
    checkpoint)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        item = await session.get(WorkQueueItem, item_id)
        if item is None:
            return
        try:
            actor = str((item.payload or {}).get("actor") or "system")

            # Void queued siblings (a pending `start`/`continue`/`cancel`): with
            # the run cancelled, no other dispatch may drive it. The cancel item
            # itself is `claimed`, not pending — it survives this update.
            voided = await session.execute(
                update(WorkQueueItem)
                .where(WorkQueueItem.run_id == item.run_id,
                       WorkQueueItem.state == "pending")
                .values(state="done"))
            await session.commit()
            if voided.rowcount:
                logger.info("cancel run=%s: voided %s pending sibling item(s)",
                            item.run_id, voided.rowcount)

            active = _active.get(item.run_id)
            if active is not None:
                active["event"].set()
                logger.info("cancel run=%s: dispatch signaled — waiting up to %ss",
                            item.run_id, config.engine.cancel_wait_seconds)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(active["task"]),
                        timeout=config.engine.cancel_wait_seconds)
                except asyncio.TimeoutError:
                    logger.warning("cancel run=%s: dispatch did not finish in %ss — "
                                   "forcing terminal state",
                                   item.run_id, config.engine.cancel_wait_seconds)
                except Exception:
                    logger.exception("cancel run=%s: dispatch raised while cancelling",
                                     item.run_id)

            # Fresh read: the dispatch may have just recorded the terminal state.
            run = await session.get(Run, item.run_id)
            if run is None:
                logger.warning("cancel item %s: run row missing", item_id)
                return
            if run.state in TERMINAL_STATES:
                logger.info("cancel run=%s: run already terminal (%s)",
                            item.run_id, run.state)
                return

            # No live dispatch (or it never finished): record cancelled directly.
            # Close the open gate first so the timeline shows it resolved, not
            # dangling in awaiting_approval.
            if run.state == "paused":
                gate_row = await _last_gate_transition(session, run.id)
                if gate_row is not None:
                    record_transition(
                        session, run.id, ExecState.AWAITING_APPROVAL,
                        ExecState.COMPLETED, step_id=gate_row.step_id,
                        attempt_no=gate_row.attempt_no, actor=actor,
                        result_status=gate_row.result_status,
                        reason=f"gate closed — run cancelled by {actor}")
            prev = run.state
            run.state = "cancelled"
            record_transition(session, run.id, prev, "cancelled",
                              reason=f"cancelled by {actor} (stop requested)")
            await session.commit()
            logger.info("cancel run=%s: recorded %s -> cancelled (by %s)",
                        item.run_id, prev, actor)
            await _publish_event({"type": "run_cancelled", "run_id": str(run.id)})
        finally:
            item.state = "done"
            await session.commit()
            METRICS.active_dispatches = len(_dispatches)


async def _heartbeat_loop(config: AppConfig) -> None:
    """Periodically update heartbeat_at for all items this engine has claimed.

    A dispatch can run for minutes (a step deadline is typically 900s) — the
    heartbeat keeps recovery from treating its claim as stale while it works.
    """
    while True:
        await asyncio.sleep(config.engine.heartbeat_interval_seconds)
        sessionmaker = get_sessionmaker()
        if sessionmaker is None:
            continue
        try:
            async with sessionmaker() as session:
                now = datetime.now(timezone.utc)
                stmt = (
                    update(WorkQueueItem)
                    .where(
                        WorkQueueItem.state == "claimed",
                        WorkQueueItem.claimed_by == _claim_identity(config),
                    )
                    .values(heartbeat_at=now)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.exception("Heartbeat update failed")
