"""Integration — project description editing + platform ADMIN membership bypass.

Covers the Configuration → Details sub-tab permissions:

- PM PATCH /api/projects/{pid} {description} persists + surfaces on GET.
- A developer membership cannot edit the description (403).
- Renaming stays ADMIN-only: PM name PATCH 403s while description 200s.
- Platform ADMINs without ANY membership row get full project access:
  catalog read, project workflow PATCH, project description PATCH, and the
  PM copy-to-project endpoint all pass the bypass.

Loop scoping: same pattern as test_workflow_catalog.py — a dedicated engine
on the test's own loop, the platform app on TestClient's portal loop. The
dev user starts at platform_role="USER" — tests promote when needed.
"""

import uuid

import pytest
import pytest_asyncio
from bheembhai.models.project import Project
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# Dedicated engine for the test's OWN loop (see module docstring).
_engine = create_async_engine(TEST_DB_URL)
_sm = async_sessionmaker(_engine, expire_on_commit=False)

# The DEV_AUTH_BYPASS identity (dependencies.py) — memberships must resolve to it.
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
    """Dev user at platform_role="USER" with a PM project + a developer project."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one_or_none()
        if user is None:
            user = User(external_id=DEV_USER[0], auth_provider=DEV_USER[1],
                        email="dev@bheembhai.local", display_name="Dev User")
            session.add(user)
            await session.flush()
        prev_role = user.platform_role
        user.platform_role = "USER"

        pm_project = Project(name=f"pmproj-{suffix}", owner_id=user.id)
        dev_project = Project(name=f"devproj-{suffix}", owner_id=user.id)
        session.add_all([pm_project, dev_project])
        await session.flush()

        pm_membership = Membership(user_id=user.id, project_id=pm_project.id,
                                   role="project_manager")
        dev_membership = Membership(user_id=user.id, project_id=dev_project.id,
                                    role="developer")
        session.add_all([pm_membership, dev_membership])

        await session.commit()
        return {
            "pm_project": pm_project.id,
            "dev_project": dev_project.id,
            "pm_membership_id": pm_membership.id,
            "user": user.id,
            "prev_role": prev_role,
            "suffix": suffix,
        }


async def _cleanup(world: dict, workflow_ids: list | None = None) -> None:
    """Delete both projects (DB cascade removes their memberships) and any
    platform workflows created by the test; restore the dev user's role."""
    async with _sm() as session:
        await session.execute(delete(Project).where(Project.id.in_(
            [world["pm_project"], world["dev_project"]])))
        for wf_id in (workflow_ids or []):
            await session.execute(delete(Policy).where(Policy.workflow_id == wf_id))
            await session.execute(delete(Workflow).where(Workflow.id == wf_id))
        user = await session.get(User, world["user"])
        if user is not None:
            user.platform_role = world["prev_role"]
        await session.commit()


async def _add_workflow(project_id, name, *, category_id=None) -> uuid.UUID:
    """Insert a workflow directly (project-scoped or platform template)."""
    async with _sm() as session:
        wf = Workflow(
            project_id=project_id, name=name, description="",
            yaml_content=(
                f"workflow: {name}\n"
                "version: 1\n"
                "start: story-design\n"
                "steps:\n"
                "  - id: story-design\n"
                "    skill: story-design\n"
                "    model: high\n"
                "    \"on\":\n"
                "      completed: DONE\n"
            ),
            is_active=True, workflow_category_id=category_id,
        )
        session.add(wf)
        await session.commit()
        return wf.id


# ── Description editing ───────────────────────────────────────────────────────


async def test_pm_can_edit_project_description(client):
    world = await _make_world()
    pid = world["pm_project"]
    try:
        resp = client.patch(f"/api/projects/{pid}",
                            json={"description": "  Built by the PM team  "})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Built by the PM team"

        # Persisted and surfaced on the auth-only GET (Details sub-tab).
        async with _sm() as session:
            project = await session.get(Project, pid)
            assert project.description == "Built by the PM team"
        resp = client.get(f"/api/projects/{pid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Built by the PM team"
    finally:
        await _cleanup(world)


async def test_developer_cannot_edit_project_description(client):
    world = await _make_world()
    try:
        resp = client.patch(f"/api/projects/{world['dev_project']}",
                            json={"description": "Not allowed"})
        assert resp.status_code == 403, resp.text
    finally:
        await _cleanup(world)


async def test_pm_cannot_rename_but_can_describe(client):
    world = await _make_world()
    pid = world["pm_project"]
    try:
        # Renaming stays ADMIN-only.
        resp = client.patch(f"/api/projects/{pid}", json={"name": "Renamed"})
        assert resp.status_code == 403, resp.text

        # The description path admits the PM.
        resp = client.patch(f"/api/projects/{pid}",
                            json={"description": "Still describable"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Still describable"

        # Name untouched.
        async with _sm() as session:
            project = await session.get(Project, pid)
            assert project.name == f"pmproj-{world['suffix']}"
    finally:
        await _cleanup(world)


# ── Platform ADMIN bypass (no membership row) ─────────────────────────────────


async def test_platform_admin_bypasses_membership_checks(client):
    world = await _make_world()
    suffix = world["suffix"]
    pid = world["pm_project"]
    wf_ids = []
    try:
        wf = await _add_workflow(pid, f"proj-wf-{suffix}")
        template = await _add_workflow(None, f"tpl-wf-{suffix}")
        wf_ids.append(template)

        # Promote to ADMIN and drop the PM membership — full project access
        # must survive without a membership row.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "ADMIN"
            await session.execute(delete(Membership).where(
                Membership.id == world["pm_membership_id"]))
            await session.commit()

        # Catalog (member-gated read) → 200
        resp = client.get(f"/api/projects/{pid}/workflow-catalog")
        assert resp.status_code == 200, resp.text

        # Project workflow PATCH (PM-gated write) → 200
        resp = client.patch(f"/api/workflows/{wf}",
                            json={"description": "Edited by admin"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Edited by admin"

        # Project description PATCH → 200
        resp = client.patch(f"/api/projects/{pid}",
                            json={"description": "Admin described"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Admin described"

        # PM copy-to-project (platform template → target project) → 201
        resp = client.post(f"/api/workflows/{template}/copy-to-project",
                           json={"project_id": str(pid)})
        assert resp.status_code == 201, resp.text
    finally:
        # Restore the PM membership + role before cleanup.
        async with _sm() as session:
            session.add(Membership(
                user_id=world["user"], project_id=pid,
                role="project_manager"))
            user = await session.get(User, world["user"])
            if user is not None:
                user.platform_role = world["prev_role"]
            await session.commit()
        await _cleanup(world, workflow_ids=wf_ids)
