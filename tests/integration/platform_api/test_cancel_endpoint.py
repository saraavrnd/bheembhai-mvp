"""Integration — POST /api/runs/{id}/cancel enqueues a `cancel` dispatch token.

The platform is a bookkeeper (ADR-013 §1): stop is a request, not a mutation.
The endpoint validates state (404 unknown run, 409 already-terminal) and
INSERTs a work_queue `cancel` item; the engine performs the cancellation.
Loop scoping: same pattern as test_create_run.py — a dedicated engine on the
test's own loop, the platform app on TestClient's portal loop.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run
from bheembhai.models.user import Membership, User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
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
      completed: DONE
"""

POLICY_YAML = """
policy: fast
version: 1
gates: {}
"""

# Dedicated engine for the test's OWN loop (see module docstring).
_engine = create_async_engine(TEST_DB_URL)
_sm = async_sessionmaker(_engine, expire_on_commit=False)

# The DEV_AUTH_BYPASS identity (dependencies.py) — membership must resolve to it.
DEV_USER = ("dev-user", "dev")


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


async def _make_world() -> dict:
    """User (the dev identity), project, workflow, policy, verified github + ai
    integrations, and a membership so POST /api/runs passes auth + membership."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        # The dev-identity user is shared across suites (created on first
        # request by get_or_create_user) — get-or-create, never delete.
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one_or_none()
        if user is None:
            user = User(external_id=DEV_USER[0], auth_provider=DEV_USER[1],
                        email="dev@bheembhai.local", display_name="Dev User")
            session.add(user)
            await session.flush()

        project = Project(name=f"cancel-proj-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        session.add(Membership(user_id=user.id, project_id=project.id,
                               role="developer"))

        workflow = Workflow(
            name=f"cancel-wf-{suffix}", version=1,
            yaml_content=WORKFLOW_YAML, project_id=project.id,
        )
        session.add(workflow)
        await session.flush()

        policy = Policy(
            name=f"cancel-pol-{suffix}", version=1,
            yaml_content=POLICY_YAML, workflow_id=workflow.id, project_id=project.id,
        )
        session.add(policy)
        await session.flush()

        github = ProjectIntegration(
            project_id=project.id, type="github", label=f"gh-{suffix}",
            credential_ref=f"gh-ref-{suffix}",
            config={"url": "https://github.com", "repository": "acme/demo",
                    "base_branch": "main"},
            verified_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        session.add(github)
        ai_vendor = ProjectIntegration(
            project_id=project.id, type="claude", label=f"claude-{suffix}",
            credential_ref=f"vendor-ref-{suffix}",
            config={"model_high": "claude-A", "model_medium": "claude-B",
                    "model_low": "claude-C"},
            verified_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        session.add(ai_vendor)
        await session.flush()

        await session.commit()
        world = {
            "project_id": project.id, "workflow_id": workflow.id,
            "policy_id": policy.id, "github_id": github.id, "ai_id": ai_vendor.id,
            "cleanup": ((Policy, policy.id), (Workflow, workflow.id),
                        (Project, project.id)),
        }
    return world


async def _cleanup(world: dict, created_runs: list[uuid.UUID]) -> None:
    async with _sm() as session:
        for run_id in created_runs:
            await session.execute(delete(WorkQueueItem).where(
                WorkQueueItem.run_id == run_id))
            await session.execute(delete(Run).where(Run.id == run_id))
        for model, row_id in world["cleanup"]:
            await session.execute(delete(model).where(model.id == row_id))
        await session.commit()


def _create_body(world: dict, **overrides) -> dict:
    body = {
        "project_id": str(world["project_id"]),
        "workflow_id": str(world["workflow_id"]),
        "policy_id": str(world["policy_id"]),
        "story_id": "LNPRTL-777",
        "github_integration_id": str(world["github_id"]),
        "ai_vendor_integration_id": str(world["ai_id"]),
    }
    body.update(overrides)
    return body


async def test_cancel_queues_token_without_state_mutation(client):
    world = await _make_world()
    created = []
    try:
        resp = client.post("/api/runs", json=_create_body(world))
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["id"]
        created.append(uuid.UUID(run_id))

        resp2 = client.post(f"/api/runs/{run_id}/cancel")
        assert resp2.status_code == 202, resp2.text
        assert resp2.json()["id"] == run_id

        async with _sm() as session:
            run = await session.get(Run, uuid.UUID(run_id))
            assert run.state == "pending"      # bookkeeper: enqueued, not mutated
            row = (await session.execute(
                select(WorkQueueItem).where(
                    WorkQueueItem.run_id == uuid.UUID(run_id),
                    WorkQueueItem.action == "cancel"))).scalar_one()
            assert row.state == "pending"
            # The actor is the dev identity (DEV_AUTH_BYPASS) — auditable.
            assert row.payload.get("actor") == "dev@bheembhai.local"
    finally:
        await _cleanup(world, created)


async def test_cancel_terminal_run_conflicts(client):
    world = await _make_world()
    created = []
    try:
        resp = client.post("/api/runs", json=_create_body(world))
        run_id = uuid.UUID(resp.json()["id"])
        created.append(run_id)

        async with _sm() as session:
            run = await session.get(Run, run_id)
            run.state = "completed"
            await session.commit()

        resp2 = client.post(f"/api/runs/{run_id}/cancel")
        assert resp2.status_code == 409
        assert "already finished" in resp2.json()["detail"]

        # And nothing was enqueued for the terminal run.
        async with _sm() as session:
            rows = (await session.execute(
                select(WorkQueueItem).where(
                    WorkQueueItem.run_id == run_id,
                    WorkQueueItem.action == "cancel"))).scalars().all()
            assert rows == []
    finally:
        await _cleanup(world, created)


async def test_cancel_unknown_run_404(client):
    resp = client.post(f"/api/runs/{uuid.uuid4()}/cancel")
    assert resp.status_code == 404
