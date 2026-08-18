"""Integration tests — stop-run (cancel) semantics against real Postgres.

A stop request travels as a `cancel` work_queue token (dispatch tokens, ADR-003):
the worker claims it OUTSIDE the per-run lock, signals the run's in-flight
dispatch through an in-memory event, and the dispatch records the terminal
state at its next checkpoint (sub-second — the reconciler polls every 0.4s).
With no live dispatch (paused at a gate / never started), the cancel handler
records the terminal state itself, closing any open gate and voiding queued
siblings.

Cross-engine: a cancel token claimed in a process that does NOT own the
dispatch has no event to signal — there the handler writes runs.state
directly and the remote dispatch observes the DB at every checkpoint.

No Docker containers are launched: FakeRuntime stands in for the Runtime
protocol (the `hung` behaviour keeps a step "running" until the cancel lands).
"""

import asyncio
import inspect
import time
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from bheembhai.config import DatabaseConfig
from bheembhai.database import (
    close_database,
    get_sessionmaker,
    init_database,
    run_migrations,
)
from bheembhai.models.run import Transition
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.providers.env_secrets import EnvSecureStorage
from sqlalchemy import delete, select
from test_state_machine import (
    POLICY_FAST,
    continue_item,
    get_run,
    make_world,
    start_item,
    step_row,
)

from conftest import FakeRuntime
from engine_service import worker as worker_mod
from engine_service.runtime import CANCELLED
from engine_service.state_machine import drive_run
from engine_service.workflow import ExecState

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# Short deadline so a cross-engine cancel (which the reconciler cannot see —
# only the post-step DB check) lands within the test, not after 900s.
WF_SHORT_DEADLINE = """
workflow: short
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    deadline: 2
    "on":
      completed: DONE
"""


# ── DB fixtures (mirror test_state_machine.py) ─────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _engine_db():
    """Point the global DB module at the dedicated test database."""
    init_database(DatabaseConfig(url=TEST_DB_URL))
    await run_migrations()
    yield
    await close_database()


@pytest_asyncio.fixture(loop_scope="session")
async def session():
    sm = get_sessionmaker()
    assert sm is not None, "database not initialised"
    created: list = []
    async with sm() as s:
        yield s, created
        await s.rollback()
    async with sm() as s2:
        for model, obj_id in reversed(created):
            await s2.execute(delete(model).where(model.id == obj_id))
        await s2.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def secure_storage():
    return EnvSecureStorage()


@pytest.fixture
def config(app_config):
    return app_config


def _collector(events):
    """Async publish callback collecting engine events into `events`."""
    async def _append(event):
        events.append(event)
    return _append


async def _claim(item, session, config):
    """Persist an item and mark it claimed by THIS process (worker_loop's path)."""
    session.add(item)
    await session.commit()
    await session.refresh(item)
    item.state = "claimed"
    item.claimed_by = worker_mod._claim_identity(config)
    item.claimed_at = datetime.now(timezone.utc)
    item.heartbeat_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(item)


async def _wait_until(predicate, timeout=10.0, interval=0.05):
    """Poll `predicate` (sync bool or async bool) until true or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


# ── Tests ──────────────────────────────────────────────────────────────

async def test_cancel_mid_step_signals_dispatch_and_stops_container(
        session, secure_storage, config):
    """The live-dispatch path: a cancel token claimed while a step is running
    signals the in-memory event; the reconciler aborts within one poll tick,
    the container is force-stopped, and the dispatch records `cancelled`."""
    s, created = session
    world = await make_world(s, created, secure_storage, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["hung"]})
    events = []
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage,
                                publish=_collector(events))

    item = start_item(world["run"])
    await _claim(item, s, config)
    await worker_mod._process_item(s, item, config)   # registers _active + spawns

    try:
        await _wait_until(lambda: len(rt.calls) > 0)   # container is up

        # A gate decision enqueued meanwhile must be voided by the cancel.
        sib = continue_item(world["run"], {"action": "approve", "actor": "x@y.co"})
        s.add(sib)
        await s.commit()

        cancel = WorkQueueItem(run_id=world["run"].id, action="cancel",
                               payload={"actor": "dev@bheembhai.local"})
        await _claim(cancel, s, config)
        await worker_mod._cancel_guarded(config, cancel.id)

        run = await get_run(s, world["run"].id)
        assert run.state == "cancelled"
        # The container was force-stopped (stop(), not the keep_containers-
        # honouring cleanup()).
        assert len(rt.stopped) == 1
        step = await step_row(s, run.id, "story-design")
        assert step.exec_state == ExecState.FAILED
        assert step.result_status == CANCELLED
        # Sibling decision voided; start + cancel items done.
        await s.refresh(sib)
        await s.refresh(item)
        await s.refresh(cancel)
        assert sib.state == "done"
        assert item.state == "done"
        assert cancel.state == "done"
        # Nothing else ever ran, and the platform was told.
        assert [c[0] for c in rt.calls] == ["story-design"]
        assert any(e["type"] == "run_cancelled" for e in events)
    finally:
        # Never leak a hung dispatch between tests.
        active = worker_mod._active.pop(world["run"].id, None)
        if active is not None:
            active["event"].set()
            try:
                await asyncio.wait_for(asyncio.shield(active["task"]), timeout=10)
            except Exception:  # noqa: S110, BLE001 — teardown: never mask the test's real result
                pass


async def test_cancel_at_open_gate_closes_gate_and_voids_decision(
        session, secure_storage, config):
    """No live dispatch (run paused at a gate): the cancel handler closes the
    open gate, voids the pending decision item, and records cancelled."""
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"]})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    # Drive to the gate the normal way (a plain dispatch, no _active entry).
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)
    run = await get_run(s, world["run"].id)
    assert run.state == "paused"

    decision = continue_item(world["run"], {"action": "approve",
                                            "actor": "reviewer@test.co"})
    s.add(decision)
    await s.commit()

    cancel = WorkQueueItem(run_id=world["run"].id, action="cancel",
                           payload={"actor": "dev@bheembhai.local"})
    await _claim(cancel, s, config)
    await worker_mod._cancel_guarded(config, cancel.id)

    run = await get_run(s, world["run"].id)
    assert run.state == "cancelled"
    await s.refresh(decision)
    assert decision.state == "done"        # voided — the decision never ran
    # The gate was closed with the cancelling actor on the record — it must
    # not dangle open in awaiting_approval.
    closed = await s.execute(
        select(Transition).where(
            Transition.run_id == run.id,
            Transition.from_state == ExecState.AWAITING_APPROVAL,
            Transition.to_state == ExecState.COMPLETED))
    t = closed.scalar_one()
    assert "gate closed" in t.reason
    assert t.actor == "dev@bheembhai.local"
    term = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "cancelled"))
    assert term.scalars().first() is not None


async def test_cancel_before_start_voids_start_item_and_records_cancelled(
        session, secure_storage, config):
    """Stop requested before the engine ever claimed the `start` item: the
    handler voids it and records cancelled directly from `pending`."""
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item = start_item(world["run"])
    s.add(item)                     # pending, unclaimed
    await s.commit()

    cancel = WorkQueueItem(run_id=world["run"].id, action="cancel",
                           payload={"actor": "dev@bheembhai.local"})
    await _claim(cancel, s, config)
    await worker_mod._cancel_guarded(config, cancel.id)

    run = await get_run(s, world["run"].id)
    assert run.state == "cancelled"
    await s.refresh(item)
    assert item.state == "done"     # the start token is dead — nothing will drive it
    assert rt.calls == []           # no container ever launched

    # A second (double-clicked) cancel is a no-op on the terminal run.
    cancel2 = WorkQueueItem(run_id=world["run"].id, action="cancel", payload={})
    await _claim(cancel2, s, config)
    await worker_mod._cancel_guarded(config, cancel2.id)
    run = await get_run(s, world["run"].id)
    assert run.state == "cancelled"
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "cancelled"))
    assert len(res.scalars().all()) == 1


async def test_preset_cancel_event_cancels_before_any_step_runs(
        session, secure_storage, config):
    """Between-steps checkpoint: a dispatch whose cancel event is already set
    records cancelled at the loop top — no step, no container."""
    s, created = session
    world = await make_world(s, created, secure_storage, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({})
    events = []
    ev = asyncio.Event()
    ev.set()
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    cancel_event=ev, publish=_collector(events))
    run = await get_run(s, world["run"].id)
    assert run.state == "cancelled"
    assert rt.calls == []
    assert any(e["type"] == "run_cancelled" for e in events)


async def test_cross_engine_cancel_force_writes_and_dispatch_observes(
        session, secure_storage, config):
    """The cancel token claimed by a process that does NOT own the dispatch
    (no _active entry): the handler records cancelled directly; the remote
    dispatch observes the DB at its next checkpoint, stops the container, and
    never launches the next step."""
    s, created = session
    world = await make_world(s, created, secure_storage,
                             wf_yaml=WF_SHORT_DEADLINE, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["hung"]})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item = start_item(world["run"])
    await _claim(item, s, config)
    # Cross-engine dispatch: spawned directly — no cancel_event, no _active entry.
    task = asyncio.create_task(worker_mod._dispatch_guarded(config, item.id))

    try:
        await _wait_until(lambda: len(rt.calls) > 0)   # container is up

        cancel = WorkQueueItem(run_id=world["run"].id, action="cancel",
                               payload={"actor": "dev@bheembhai.local"})
        await _claim(cancel, s, config)
        await worker_mod._cancel_guarded(config, cancel.id)   # nothing to signal

        await task     # the dispatch aborts when it observes the DB (≤ deadline 2s)

        run = await get_run(s, world["run"].id)
        assert run.state == "cancelled"
        assert len(rt.stopped) == 1      # observed-cancel still stops the container
        step = await step_row(s, run.id, "story-design")
        assert step.result_status == CANCELLED
        # The next step was never launched.
        assert [c[0] for c in rt.calls] == ["story-design"]
        await s.refresh(item)
        assert item.state == "done"
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
