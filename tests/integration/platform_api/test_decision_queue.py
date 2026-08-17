"""Integration — POST /api/runs/{id}/decision enqueues a continue item (ADR-003).

Plan decision #8: the platform no longer mutates run state at a gate decision.
It validates the run is actually paused, then inserts a WorkQueueItem(action=
"continue", payload={action, send_back_to, comment, actor}) for the engine to
claim. The UI keeps polling and sees the state flip when the engine acts.

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
from bheembhai.models.run import Run
from bheembhai.models.user import User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow

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

# Dedicated engine for the test's OWN loop (see module docstring). Created on
# first use — create_async_engine connects lazily, so the pool binds to
# whatever loop runs the first query, which is the session loop.
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
    """Insert minimal world rows; return the Run plus its cleanup ids."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        user = User(
            external_id=f"decision-owner-{suffix}",
            auth_provider="dev",
            email=f"decision-owner-{suffix}@bheembhai.local",
            display_name="Decision Test Owner",
        )
        session.add(user)
        await session.flush()

        project = Project(name=f"decision-project-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        workflow = Workflow(
            name=f"decision-wf-{suffix}", version=1,
            yaml_content=WORKFLOW_YAML, project_id=project.id,
        )
        session.add(workflow)
        await session.flush()

        policy = Policy(
            name=f"decision-pol-{suffix}", version=1,
            yaml_content=POLICY_YAML, workflow_id=workflow.id, project_id=project.id,
        )
        session.add(policy)
        await session.flush()

        run = Run(
            project_id=project.id, workflow_id=workflow.id, policy_id=policy.id,
            source_branch="main", state=state, started_by_user_id=user.id,
        )
        session.add(run)
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
        await session.execute(delete(Run).where(Run.id == world._bb_run_id))
        for model, row_id in world._bb_cleanup:
            await session.execute(delete(model).where(model.id == row_id))
        await session.commit()


async def _items(run_id) -> list[WorkQueueItem]:
    async with _sm() as session:
        rows = (await session.execute(
            select(WorkQueueItem).where(WorkQueueItem.run_id == run_id)
        )).scalars().all()
        return list(rows)


async def _run_state(run_id) -> str:
    async with _sm() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run.state


async def test_approve_enqueues_continue_item_without_state_change(client):
    run = await _make_world(state="paused")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={"action": "approve"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == str(run.id)
        assert body["decision"] == "approve"
        assert "queued" in body["message"]

        items = await _items(run.id)
        assert len(items) == 1
        item = items[0]
        assert item.action == "continue"
        assert item.state == "pending"
        assert item.payload == {
            "action": "approve",
            "comment": "",
            "actor": "dev@bheembhai.local",
        }
        # The platform must NOT flip state — the engine does, on claim.
        assert await _run_state(run.id) == "paused"
    finally:
        await _cleanup(run)


async def test_send_back_enqueues_target_and_comment(client):
    run = await _make_world(state="paused")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={
            "action": "send_back", "send_back_to": "story-design", "comment": "redo it",
        })
        assert resp.status_code == 200
        assert resp.json()["decision"] == "send_back"

        items = await _items(run.id)
        assert len(items) == 1
        assert items[0].payload == {
            "action": "send_back",
            "send_back_to": "story-design",
            "comment": "redo it",
            "actor": "dev@bheembhai.local",
        }
        assert await _run_state(run.id) == "paused"
    finally:
        await _cleanup(run)


async def test_decision_on_non_paused_run_conflicts(client):
    run = await _make_world(state="running")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={"action": "approve"})
        assert resp.status_code == 409
        assert await _items(run.id) == []
        assert await _run_state(run.id) == "running"
    finally:
        await _cleanup(run)


async def test_send_back_unknown_step_rejected(client):
    run = await _make_world(state="paused")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={
            "action": "send_back", "send_back_to": "nope",
        })
        assert resp.status_code == 400
        assert await _items(run.id) == []
    finally:
        await _cleanup(run)


async def test_send_back_without_target_rejected(client):
    run = await _make_world(state="paused")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={"action": "send_back"})
        assert resp.status_code == 400
        assert await _items(run.id) == []
    finally:
        await _cleanup(run)


async def test_unknown_action_rejected(client):
    run = await _make_world(state="paused")
    try:
        resp = client.post(f"/api/runs/{run.id}/decision", json={"action": "explode"})
        assert resp.status_code == 400
        assert await _items(run.id) == []
    finally:
        await _cleanup(run)
