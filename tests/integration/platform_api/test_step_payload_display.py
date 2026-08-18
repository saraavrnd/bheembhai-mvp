"""Integration — completed/approved stages still render the engine's real payload.

The engine records gate approval as an ``awaiting_approval→completed``
transition with an EMPTY payload. The display lookup must skip that row and
render the gate card / completion payload instead — otherwise the UI falls
back to the demo stubs (the "Okta" story) for an approved step. Regression
for the approved-story-design-shows-stub bug.

Loop scoping: the platform app runs on TestClient's portal loop, while these
tests use their own DB engine on the session-scoped pytest-asyncio loop.
asyncpg connections are loop-bound, so the two MUST NOT share a pool — hence
the dedicated `_engine`/`_sm` here instead of `get_sessionmaker()`.
"""

import uuid

import pytest
import pytest_asyncio
from bheembhai.models.project import Project
from bheembhai.models.run import Run, Step, Transition
from bheembhai.models.user import User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
      completed: DONE
"""

POLICY_YAML = """
policy: governed
version: 1
gates: []
"""

REAL_PATH = "docs/product/epics/LNPRTL-9/stories/LNPRTL-50/story-design.md"
GATE_SUMMARY = "The story-design note is written. Here's the summary for the reviewer."

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
    """Insert minimal world rows plus the story-design transition history:
    completion payload → gate card → empty approval record (newest last,
    exactly what the engine writes when a gate decision is applied)."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        user = User(
            external_id=f"payload-owner-{suffix}",
            auth_provider="dev",
            email=f"payload-owner-{suffix}@bheembhai.local",
            display_name="Payload Test Owner",
        )
        session.add(user)
        await session.flush()

        project = Project(name=f"payload-project-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        workflow = Workflow(
            name=f"payload-wf-{suffix}", version=1,
            yaml_content=WORKFLOW_YAML, project_id=project.id,
        )
        session.add(workflow)
        await session.flush()

        policy = Policy(
            name=f"payload-pol-{suffix}", version=1,
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

        session.add(Step(
            run_id=run.id, step_id="story-design", skill="story-design",
            exec_state="completed", result_status="completed",
        ))
        await session.flush()

        artifact = {"summary": GATE_SUMMARY, "commit": "abc1234",
                    "files": [{"path": REAL_PATH, "note": ""}],
                    "review_files": [{"path": REAL_PATH, "note": ""}]}
        # 1. step completion payload
        session.add(Transition(
            run_id=run.id, step_id="story-design", attempt_no=1,
            from_state="awaiting_result", to_state="completed",
            payload=artifact, ts=1000.0,
        ))
        # 2. gate card (what the UI renders while paused)
        session.add(Transition(
            run_id=run.id, step_id="story-design", attempt_no=1,
            from_state="completed", to_state="awaiting_approval",
            payload={**artifact, "role": "project_manager",
                     "result_status": "completed", "reason": "gate"},
            ts=1001.0,
        ))
        # 3. gate approval — newest row, NO payload (the bug trigger)
        session.add(Transition(
            run_id=run.id, step_id="story-design", attempt_no=1,
            from_state="awaiting_approval", to_state="completed",
            payload=None, actor="reviewer@bheembhai.local", ts=1002.0,
        ))
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


async def test_approved_gate_still_renders_real_payload(client):
    run = await _make_world(state="running")
    try:
        resp = client.get(f"/api/runs/{run.id}")
        assert resp.status_code == 200
        stages = resp.json()["stages"]
        sd = next(s for s in stages if s["step_id"] == "story-design")
        assert [f["path"] for f in sd["files"]] == [REAL_PATH], (
            "approved gate must render the engine payload, not the demo stubs"
        )
        assert sd["summary"] == GATE_SUMMARY
        assert sd["commit"] == "abc1234"
    finally:
        await _cleanup(run)


async def test_latest_step_payload_skips_empty_rows():
    from platform_api.routers.runs import _latest_step_payload

    run = await _make_world(state="running")
    try:
        async with _sm() as session:
            payload = await _latest_step_payload(session, run._bb_run_id, "story-design")
        assert payload.get("summary") == GATE_SUMMARY
        assert payload.get("commit") == "abc1234"
        # A step with ONLY an empty approval record yields nothing.
        async with _sm() as session:
            await session.execute(delete(Transition).where(
                Transition.run_id == run._bb_run_id))
            session.add(Transition(
                run_id=run._bb_run_id, step_id="story-design", attempt_no=1,
                from_state="awaiting_approval", to_state="completed",
                payload=None, ts=2000.0,
            ))
            await session.commit()
        async with _sm() as session:
            payload = await _latest_step_payload(session, run._bb_run_id, "story-design")
        assert payload == {}
    finally:
        await _cleanup(run)
