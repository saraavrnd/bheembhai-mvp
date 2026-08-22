"""Integration tests — ad-hoc interactive sessions (ADR-016) against real Postgres.

The session model: a run with run_kind="adhoc" is paused at `awaiting_input`
between turns (never at a gate — sessions have no gates). One dispatch = one
turn = one pause. The step row stays exec_state="running" for the whole
session: `attempt_no` numbers CONTAINER incarnations (the reaper's
cold-starts) while `turn_no` numbers turns across incarnations — the global
seq the inbox/outbox match on. Each completed turn commits a Transition row
{kind:"turn", seq, query, response, commit, files, cost} — the durable,
auditable turn history independent of object storage.

No Docker containers are launched: FakeRuntime's `session` behaviour stands in
for the agent process — a background actor polls the turn inbox at the real
object-store key, answers each new seq with an outbox reply, and exits cleanly
on the `end` sentinel.
"""

import asyncio
from datetime import datetime, timedelta, timezone

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
from bheembhai.providers.env_secrets import EnvSecureStorage
from sqlalchemy import delete, select
from test_state_machine import (
    POLICY_FAST,
    _collector,
    _store,
    _wait_until,
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
from engine_service.workflow import ExecState, Result

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# 1-step workflow + no gates — the ad-hoc world. Sessions never pause at a
# gate; a completed turn pauses at awaiting_input and DONE terminates the loop.
WF_ADHOC = """
workflow: adhoc
start: adhoc
steps:
  - id: adhoc
    skill: adhoc
    model: high
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


def _make_adhoc(s, created, secure_storage, *, state="pending"):
    """An ad-hoc world: 1-step 'adhoc' workflow, no gates, a pre-set user
    branch (init skips the network), and the session's opening query on the
    run — the first turn's prompt when starting from `pending`."""
    return make_world(
        s, created, secure_storage, wf_yaml=WF_ADHOC, pol_yaml=POLICY_FAST,
        state=state, run_kind="adhoc", user_query="open the session")


# ── Session lifecycle ───────────────────────────────────────────────────

async def test_session_turns_pause_at_awaiting_input_then_end(session,
                                                              secure_storage,
                                                              config, tmp_path):
    s, created = session
    world = await _make_adhoc(s, created, secure_storage)
    store = _store(tmp_path)
    rt = FakeRuntime({"adhoc": ["session"]}, store=store,
                     reattach_script={"adhoc": "session"})
    events = []
    try:
        # The start token dispatches the opening turn (run.user_query).
        await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                        publish=_collector(events), store=store)

        run = await get_run(s, world["run"].id)
        assert run.state == "paused"          # awaiting_input, not a gate
        assert run.session_phase == "active"
        # The engine minted the session id at init (ADR-016 §3) — every
        # incarnation launches with it.
        assert run.claude_session_id is not None
        row = await step_row(s, run.id, "adhoc")
        assert row.exec_state == ExecState.RUNNING   # the live container IS the session
        assert row.attempt_no == 1
        assert row.turn_no == 1

        # Turn 2 re-attaches the SAME container (no second launch).
        await drive_run(s, continue_item(world["run"],
                                         {"action": "turn", "query": "second thing"}),
                        config, rt, secure_storage, publish=_collector(events),
                        store=store)

        run = await get_run(s, world["run"].id)
        assert run.state == "paused"
        row = await step_row(s, run.id, "adhoc")
        assert row.attempt_no == 1            # still the same incarnation
        assert row.turn_no == 2
        assert [c for c in rt.calls] == [("adhoc", 1)]
        # Per-turn cost accrual on run + step.
        assert float(run.cost_usd) == 0.2
        assert float(row.cost_usd) == 0.2

        # Each completed turn left a durable audit row (query + response +
        # commit + cost) — the turn history independent of object storage.
        turns = (await s.execute(
            select(Transition).where(
                Transition.run_id == run.id,
                Transition.to_state == ExecState.AWAITING_INPUT)
            .order_by(Transition.id))).scalars().all()
        assert [t.payload.get("kind") for t in turns] == ["turn", "turn"]
        assert turns[0].payload["seq"] == 1
        assert turns[0].payload["query"] == "open the session"
        assert "echo: open the session" in turns[0].payload["response"]
        assert turns[0].payload["commit"]
        assert turns[0].payload["cost_usd"] == 0.1
        assert turns[1].payload["seq"] == 2
        assert turns[1].payload["query"] == "second thing"
        # The dispatch audit trail: one turn_request row per dispatched turn.
        requests = (await s.execute(
            select(Transition).where(Transition.run_id == run.id)
            .order_by(Transition.id))).scalars().all()
        kinds = [(t.payload or {}).get("kind") for t in requests]
        assert kinds.count("turn_request") == 2
        # Wide-open governance: a session never pauses at an approval gate.
        gates = (await s.execute(
            select(Transition).where(
                Transition.run_id == run.id,
                Transition.to_state == ExecState.AWAITING_APPROVAL))).scalars().all()
        assert gates == []
        assert any(e["type"] == "turn_completed" and e["seq"] == 2 for e in events)

        # Explicit End session: sentinel → clean exit → run completed.
        await drive_run(s, continue_item(world["run"], {"action": "end"}),
                        config, rt, secure_storage, publish=_collector(events),
                        store=store)

        run = await get_run(s, world["run"].id)
        assert run.state == "completed"
        assert run.session_phase == "ended"
        row = await step_row(s, run.id, "adhoc")
        assert row.exec_state == ExecState.COMPLETED
        assert row.result_status == Result.COMPLETED
        assert row.fargate_task_arn is None
        assert len(rt.cleaned) == 1        # exited cleanly, then cleaned up
        assert any(e["type"] == "run_completed" for e in events)
    finally:
        await rt.aclose()


async def test_session_reap_then_cold_start_resumes(session, secure_storage,
                                                    config, tmp_path):
    s, created = session
    world = await _make_adhoc(s, created, secure_storage)
    store = _store(tmp_path)
    rt = FakeRuntime({"adhoc": ["session", "session"]}, store=store,
                     reattach_script={"adhoc": "session"})
    events = []
    # The reaper spawns its per-run task against the WORKER's globals.
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage,
                                publish=_collector(events), store=store)
    try:
        await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                        publish=_collector(events), store=store)
        run = await get_run(s, world["run"].id)
        assert run.state == "paused"

        # Idle the session past the threshold and sweep (the worker does this
        # every loop iteration).
        run.session_last_activity_at = datetime.now(timezone.utc) - timedelta(
            seconds=config.engine.ad_hoc_idle_seconds + 60)
        await s.commit()

        reaped = await worker_mod._reap_idle_adhoc_sessions(s, config)
        assert reaped == 1

        async def _reaped():
            return not worker_mod._reap_tasks

        await _wait_until(_reaped, "reap task finishing")

        run = await get_run(s, world["run"].id)
        assert run.state == "paused"            # the run stays open for more turns
        assert run.session_phase == "ended"
        row = await step_row(s, run.id, "adhoc")
        assert row.fargate_task_arn is None
        assert row.attempt_no == 1
        assert len(rt.cleaned) == 1             # sentinel → clean exit → cleanup
        assert any(e["type"] == "session_ended" and e["reason"] == "idle"
                   for e in events)

        # The next turn cold-starts a fresh incarnation with --resume (ADR-016 §3).
        await drive_run(s, continue_item(world["run"],
                                         {"action": "turn", "query": "resume work"}),
                        config, rt, secure_storage, publish=_collector(events),
                        store=store)

        run = await get_run(s, world["run"].id)
        assert run.state == "paused"
        row = await step_row(s, run.id, "adhoc")
        assert row.attempt_no == 2              # new incarnation, not a retry
        assert row.turn_no == 2                 # the global seq keeps counting
        assert [c for c in rt.calls] == [("adhoc", 1), ("adhoc", 2)]
        # The launch env identifies the session + the resume for the cold start.
        envs = [env for sid, env in rt.envs if sid == "adhoc"]
        assert envs[0]["BB_SESSION"] == "1"
        assert envs[0]["BB_SESSION_ID"] == run.claude_session_id
        assert envs[0]["BB_SESSION_RESUME"] == "0"    # fresh incarnation
        assert "BB_TRANSCRIPT_GET_URL" not in envs[0]  # nothing to restore yet
        assert envs[1]["BB_SESSION_ID"] == run.claude_session_id
        assert envs[1]["BB_SESSION_RESUME"] == "1"    # --resume incarnation
        assert envs[1]["BB_INBOX_GET_URL"].startswith("file://")
        # The transcript GET is a GET — LocalStorage presigns it as file://.
        # PUTs cannot presign here, so the upload contract is omitted from the
        # env (ADR-014: a missing URL skips that upload, never a failure).
        assert envs[1]["BB_TRANSCRIPT_GET_URL"].startswith("file://")
        assert "BB_TRANSCRIPT_PUT_URL" not in envs[1]
    finally:
        await rt.aclose()


async def test_session_cancel_mid_turn_stops_live_container(session,
                                                            secure_storage,
                                                            config, tmp_path):
    s, created = session
    world = await _make_adhoc(s, created, secure_storage)
    store = _store(tmp_path)
    rt = FakeRuntime({"adhoc": ["session"]}, store=store,
                     reattach_script={"adhoc": "session"})
    try:
        await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                        store=store)
        run = await get_run(s, world["run"].id)
        assert run.state == "paused"

        # The stop signal lands while a turn is in flight (the worker sets the
        # in-flight dispatch's event): the reconciler aborts at its next tick,
        # the caller stops the LIVE container, and the run records the
        # terminal state.
        cancel_event = asyncio.Event()
        cancel_event.set()
        await drive_run(s, continue_item(world["run"],
                                         {"action": "turn", "query": "doomed"}),
                        config, rt, secure_storage, cancel_event=cancel_event,
                        store=store)

        run = await get_run(s, world["run"].id)
        assert run.state == "cancelled"
        row = await step_row(s, run.id, "adhoc")
        assert row.fargate_task_arn is None
        assert len(rt.stopped) == 1              # the live session container was stopped
        assert rt.stopped[0].behaviour == "gone"  # its actor wound down with it
        res = await s.execute(
            select(Transition).where(
                Transition.run_id == run.id,
                Transition.result_status == CANCELLED)
            .limit(1))
        assert res.scalar_one_or_none() is not None
    finally:
        await rt.aclose()
