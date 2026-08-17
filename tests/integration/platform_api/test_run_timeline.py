"""Integration — the run detail response carries the true execution timeline.

Builds a run whose code-review visit returns ``changes_requested`` so the
workflow re-runs implement (which then fails ``failed_init``), and asserts
the timeline shows BOTH implement visits in execution order — the exact
scenario the unique-stage rail got wrong (run 03ad1cd6). Also pins the
gate-decision edge and the payload-per-visit behaviour through the real
GET /api/runs/{id} response.

Loop scoping: the platform app runs on TestClient's portal loop, while these
tests use their own DB engine on the session-scoped pytest-asyncio loop.
asyncpg connections are loop-bound, so the two MUST NOT share a pool — hence
the dedicated `_engine`/`_sm` here instead of `get_sessionmaker()`.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bheembhai.models.project import Project
from bheembhai.models.run import Run, Step, Transition
from bheembhai.models.user import User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.work_queue import WorkQueueItem

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

WORKFLOW_YAML = """
workflow: story-delivery
version: 1
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: implement
  - id: implement
    skill: implement
    model: medium
    "on":
      completed: code-review
  - id: code-review
    skill: code-review
    model: high
    "on":
      completed: DONE
      changes_requested: implement
  - id: pr-create
    skill: pr-create
    model: low
    "on":
      completed: DONE
"""

POLICY_YAML = """
policy: governed
version: 1
gates:
  story-design: {review: required, role: any}
"""

STORY_FILE = "docs/story.md"
IMPL_FILE = "src/main.py"

# Dedicated engine for the test's OWN loop (see module docstring).
_engine = create_async_engine(TEST_DB_URL)
_sm = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _dispose_engine():
    yield
    await _engine.dispose()


@pytest.fixture
def client(monkeypatch):
    """Full platform app — env must be set BEFORE the lifespan loads config."""
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    from platform_api.main import app
    with TestClient(app) as c:
        yield c


async def _make_world(state: str) -> Run:
    """A run that looped: design (gated, approved) → implement ✓ →
    code-review changes_requested → implement re-run failed_init."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        user = User(
            external_id=f"timeline-owner-{suffix}",
            auth_provider="dev",
            email=f"timeline-owner-{suffix}@bheembhai.local",
            display_name="Timeline Test Owner",
        )
        session.add(user)
        await session.flush()

        project = Project(name=f"timeline-project-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        workflow = Workflow(
            name=f"timeline-wf-{suffix}", version=1,
            yaml_content=WORKFLOW_YAML, project_id=project.id,
        )
        session.add(workflow)
        await session.flush()

        policy = Policy(
            name=f"timeline-pol-{suffix}", version=1,
            yaml_content=POLICY_YAML, workflow_id=workflow.id, project_id=project.id,
        )
        session.add(policy)
        await session.flush()

        run = Run(
            project_id=project.id, workflow_id=workflow.id, policy_id=policy.id,
            source_branch="main", state=state, started_by_user_id=user.id,
        )
        session.add(run)
        await session.flush()

        for sid, exec_state, result_status in [
            ("story-design", "completed", "completed"),
            ("implement", "failed", "failed_init"),
            ("code-review", "failed", "changes_requested"),
        ]:
            session.add(Step(
                run_id=run.id, step_id=sid, skill=sid,
                exec_state=exec_state, result_status=result_status,
            ))
        await session.flush()

        def tr(step_id, fr, to, ts, **kw):
            session.add(Transition(
                run_id=run.id, step_id=step_id, attempt_no=1,
                from_state=fr, to_state=to, ts=ts, **kw,
            ))

        # story-design visit 1 — gated, approved
        tr("story-design", "pending", "running", 100.0)
        tr("story-design", "running", "awaiting_result", 101.0)
        tr("story-design", "awaiting_result", "completed", 400.0,
           result_status="completed", reason="ok",
           payload={"summary": "Story done", "commit": "abc1234",
                    "files": [{"path": STORY_FILE}]})
        tr("story-design", "completed", "awaiting_approval", 401.0,
           result_status="completed",
           payload={"result_status": "completed", "files": [{"path": STORY_FILE}]})
        tr("story-design", "awaiting_approval", "completed", 500.0,
           result_status="completed", actor="reviewer@bheembhai.local",
           reason="reviewer chose: approve", payload=None)
        # implement visit 1 — completed
        tr("implement", "pending", "running", 500.0)
        tr("implement", "running", "awaiting_result", 501.0)
        tr("implement", "awaiting_result", "completed", 900.0,
           result_status="completed", reason="ok",
           payload={"commit": "def5678", "files": [{"path": IMPL_FILE}]})
        # code-review visit 1 — changes requested
        tr("code-review", "pending", "running", 900.0)
        tr("code-review", "running", "awaiting_result", 901.0)
        tr("code-review", "awaiting_result", "failed", 1000.0,
           result_status="changes_requested", reason="ok", payload=None)
        # implement visit 2 — the re-run fails (attempt_no stays 1, like the
        # production bug — visits must come from the transition stream)
        tr("implement", "pending", "running", 1000.0)
        tr("implement", "running", "awaiting_result", 1001.0)
        tr("implement", "awaiting_result", "failed", 1002.0,
           result_status="failed_init",
           reason="could not clone https://github.com/owner/repo.git @ main")
        tr("implement", "running", "failed", 1002.0,
           result_status="failed_init",
           reason="workflow has no route for 'failed_init' from step 'implement'")
        await session.commit()

        run_id = run.id
        cleanup = ((Policy, policy.id), (Workflow, workflow.id),
                   (Project, project.id), (User, user.id))
    run._bb_cleanup = cleanup  # type: ignore[attr-defined]
    run._bb_run_id = run_id  # type: ignore[attr-defined]
    return run


async def _cleanup(world: Run) -> None:
    async with _sm() as session:
        await session.execute(delete(WorkQueueItem).where(
            WorkQueueItem.run_id == world._bb_run_id))
        # transitions + steps cascade from the run delete
        await session.execute(delete(Run).where(Run.id == world._bb_run_id))
        for model, row_id in world._bb_cleanup:
            await session.execute(delete(model).where(model.id == row_id))
        await session.commit()


async def test_timeline_shows_both_implement_visits(client):
    run = await _make_world(state="failed")
    try:
        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        body = resp.json()

        nodes = body["timeline"]["nodes"]
        assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
            ("story-design", 1), ("implement", 1), ("code-review", 1),
            ("implement", 2), ("pr-create", 0),
        ]

        by_key = {(n["step_id"], n["visit_no"]): n for n in nodes}

        # First implement visit keeps its own completed verdict and files —
        # the re-run's failure must not clobber it.
        v1 = by_key[("implement", 1)]
        assert v1["state"] == "done"
        assert v1["verdict"] == "completed"
        assert [f["path"] for f in v1["files"]] == [IMPL_FILE]

        # The re-run visit is its own node, failed, with the clone reason.
        v2 = by_key[("implement", 2)]
        assert v2["state"] == "failed"
        assert v2["verdict"] == "failed_init"
        assert "could not clone" in v2["reason"]

        # code-review's verdict chip data
        cr = by_key[("code-review", 1)]
        assert cr["state"] == "done"
        assert cr["verdict"] == "changes_requested"

        # Gate decision edge on the gated visit; its payload survives the
        # empty approval row (no demo-stub fallback).
        sd = by_key[("story-design", 1)]
        assert sd["gate_decision"]["action"] == "approve"
        assert sd["gate_decision"]["actor"] == "reviewer@bheembhai.local"
        assert [f["path"] for f in sd["files"]] == [STORY_FILE]
        assert sd["summary"] == "Story done"

        # Pending tail + terminal run → no live node
        assert by_key[("pr-create", 0)]["state"] == "pending"
        assert body["timeline"]["current_node_idx"] is None

        # The unique-stage list survives for the send-back modal.
        assert [s["step_id"] for s in body["stages"]] == [
            "story-design", "implement", "code-review", "pr-create"]
    finally:
        await _cleanup(run)


async def test_runaway_loop_halt_row_renders_as_failed_node(client):
    """Run 18a35087's failure mode: code-review requested changes and the
    engine's visit cap halted the run with a failure row addressed to
    implement AFTER code-review was the open visit — the reason must stay
    visible in the timeline."""
    run = await _make_world(state="running")
    try:
        async with _sm() as session:
            # Drop implement's re-run rows — the halt fires instead of a
            # fourth launch, like the production cap.
            await session.execute(delete(Transition).where(
                Transition.run_id == run._bb_run_id,
                Transition.step_id == "implement", Transition.ts >= 1000.0))
            session.add(Transition(
                run_id=run._bb_run_id, step_id="implement", attempt_no=1,
                from_state="running", to_state="failed", ts=1002.0,
                reason=("step 'implement' visited 4 times in one dispatch "
                        "(cap 3) — runaway loop halted, escalating for a human")))
            run_row = await session.get(Run, run._bb_run_id)
            run_row.state = "failed"
            await session.commit()

        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        nodes = resp.json()["timeline"]["nodes"]
        assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
            ("story-design", 1), ("implement", 1), ("code-review", 1),
            ("implement", 2), ("pr-create", 0),
        ]
        halt = nodes[3]
        assert halt["state"] == "failed"
        assert halt["verdict"] is None
        assert "runaway loop halted" in halt["reason"]
        # The closed code-review visit keeps its verdict.
        assert nodes[2]["verdict"] == "changes_requested"
    finally:
        await _cleanup(run)


async def test_open_gate_node_is_awaiting_with_live_index(client):
    from bheembhai.models.run import Transition as _T

    run = await _make_world(state="running")
    try:
        # Reopen the design gate: delete its approval row and set run paused —
        # the gate card is live, so the node must be awaiting + pinned.
        async with _sm() as session:
            await session.execute(delete(_T).where(
                _T.run_id == run._bb_run_id,
                _T.step_id == "story-design",
                _T.from_state == "awaiting_approval"))
            run_row = await session.get(Run, run._bb_run_id)
            run_row.state = "paused"
            await session.commit()

        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        body = resp.json()
        nodes = body["timeline"]["nodes"]
        assert nodes[0]["step_id"] == "story-design"
        assert nodes[0]["state"] == "awaiting"
        assert nodes[0]["is_awaiting_review"] is True
        assert body["timeline"]["current_node_idx"] == 0
    finally:
        await _cleanup(run)
