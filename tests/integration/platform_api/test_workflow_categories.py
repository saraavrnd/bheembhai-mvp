"""Integration — workflow categories: CRUD, workflow mapping, clone fidelity.

Covers the user-facing contract of the workflow_category feature:

- Admin CRUD on /api/admin/workflow-categories (create/update/delete, 409 on
  duplicate names, 409 on deleting an in-use category).
- Workflow create/PATCH carrying ``category_id`` (mandatory on create → 422
  when missing; clearing via PATCH rejected → 400; unknown id → 400).
- Copy-to-project (both admin and PM endpoints) preserving the source
  workflow's category id on the clone.
- GET /api/workflow-categories being plain reference data (no admin role).
- ``description`` persistence: admin create/PATCH, PM PATCH, and both clone
  endpoints carry the workflow description through to the stored row.

Loop scoping: same pattern as test_create_run.py — a dedicated engine on the
test's own loop, the platform app on TestClient's portal loop. The app's
lifespan runs alembic upgrade head on the test DB before any request.
"""

import uuid

import pytest
import pytest_asyncio
from bheembhai.models.project import Project
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.workflow_category import WorkflowCategory
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

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
    """Dev user (promoted to ADMIN) + a project they manage (PM membership).

    Returns ids for cleanup; the caller builds workflows/categories on top.
    """
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
        user.platform_role = "ADMIN"

        project = Project(name=f"catproj-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()
        session.add(Membership(user_id=user.id, project_id=project.id,
                               role="project_manager"))

        await session.commit()
        return {"project": project.id, "user": user.id, "prev_role": prev_role,
                "suffix": suffix}


async def _cleanup(world: dict, workflow_ids: list | None = None,
                   category_ids: list | None = None) -> None:
    """Delete the project (cascades project workflows), platform workflows,
    and categories created by the test; restore the dev user's role.
    """
    async with _sm() as session:
        await session.execute(delete(Project).where(
            Project.id == world["project"]))
        for wf_id in (workflow_ids or []):
            await session.execute(delete(Policy).where(Policy.workflow_id == wf_id))
            await session.execute(delete(Workflow).where(Workflow.id == wf_id))
        for cat_id in (category_ids or []):
            await session.execute(delete(WorkflowCategory).where(
                WorkflowCategory.id == cat_id))
        user = await session.get(User, world["user"])
        if user is not None:
            user.platform_role = world["prev_role"]
        await session.commit()


def _wf_yaml(name: str) -> str:
    return f"workflow: {name}\nversion: 1\nstart: ''\nsteps: []\n"


# ── Category CRUD ───────────────────────────────────────────────────────────


async def test_category_crud_and_duplicate_guard(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids = []
    try:
        # Create
        resp = client.post("/api/admin/workflow-categories",
                           json={"name": f"Category-{suffix}",
                                 "description": "test category"})
        assert resp.status_code == 201, resp.text
        cat = resp.json()
        cat_ids.append(cat["id"])
        assert cat["name"] == f"Category-{suffix}"
        assert cat["description"] == "test category"

        # Duplicate name (case-insensitive) → 409
        resp = client.post("/api/admin/workflow-categories",
                           json={"name": f"category-{suffix}"})
        assert resp.status_code == 409, resp.text

        # Update
        resp = client.patch(f"/api/admin/workflow-categories/{cat['id']}",
                            json={"name": f"Category-{suffix}-2",
                                  "description": "updated"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["name"] == f"Category-{suffix}-2"
        assert resp.json()["description"] == "updated"

        # Update to a duplicate name → 409
        second = client.post("/api/admin/workflow-categories",
                             json={"name": f"Other-{suffix}"})
        assert second.status_code == 201, second.text
        cat_ids.append(second.json()["id"])
        resp = client.patch(f"/api/admin/workflow-categories/{second.json()['id']}",
                            json={"name": f"Category-{suffix}-2"})
        assert resp.status_code == 409, resp.text

        # Update unknown id → 404
        resp = client.patch(f"/api/admin/workflow-categories/{uuid.uuid4()}",
                            json={"name": "nope"})
        assert resp.status_code == 404, resp.text

        # Listed in the admin mirror
        listed = client.get("/api/admin/workflow-categories").json()
        names = [c["name"] for c in listed]
        assert f"Category-{suffix}-2" in names

        # Delete (unused) → 204, gone from the list
        resp = client.delete(f"/api/admin/workflow-categories/{second.json()['id']}")
        assert resp.status_code == 204, resp.text
        listed = client.get("/api/admin/workflow-categories").json()
        assert f"Other-{suffix}" not in [c["name"] for c in listed]

        # Delete unknown id → 404
        resp = client.delete(f"/api/admin/workflow-categories/{uuid.uuid4()}")
        assert resp.status_code == 404, resp.text
    finally:
        await _cleanup(world, category_ids=cat_ids)


# ── Workflow create / PATCH with category ───────────────────────────────────


async def test_workflow_category_set_clear_and_validation(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids, wf_ids = [], []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Cat-{suffix}"}).json()
        cat_ids.append(cat["id"])

        # Create WITHOUT category → 422 (category is mandatory)
        resp = client.post("/api/admin/workflows",
                           json={"name": f"wf-nocat-{suffix}",
                                 "yaml_content": _wf_yaml(f"wf-nocat-{suffix}")})
        assert resp.status_code == 422, resp.text

        # Create with category → response carries id + name
        resp = client.post("/api/admin/workflows",
                           json={"name": f"wf-{suffix}",
                                 "yaml_content": _wf_yaml(f"wf-{suffix}"),
                                 "category_id": cat["id"]})
        assert resp.status_code == 201, resp.text
        wf = resp.json()
        wf_ids.append(wf["id"])
        assert wf["category_id"] == cat["id"]
        assert wf["category_name"] == f"Cat-{suffix}"

        # Create with unknown category → 400
        resp = client.post("/api/admin/workflows",
                           json={"name": f"wf-bad-{suffix}",
                                 "yaml_content": _wf_yaml(f"wf-bad-{suffix}"),
                                 "category_id": str(uuid.uuid4())})
        assert resp.status_code == 400, resp.text

        # PATCH without the key → unchanged
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"is_active": False})
        assert resp.status_code == 200, resp.text
        assert resp.json()["category_id"] == cat["id"]

        # PATCH with null → 400 (clearing is rejected), category still set
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"category_id": None})
        assert resp.status_code == 400, resp.text
        resp = client.get(f"/api/admin/workflows/{wf['id']}")
        assert resp.json()["category_id"] == cat["id"]

        # PATCH with unknown id → 400, category still set
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"category_id": str(uuid.uuid4())})
        assert resp.status_code == 400, resp.text
        resp = client.get(f"/api/admin/workflows/{wf['id']}")
        assert resp.json()["category_id"] == cat["id"]

        # PATCH set to another category
        cat2 = client.post("/api/admin/workflow-categories",
                           json={"name": f"Cat2-{suffix}"}).json()
        cat_ids.append(cat2["id"])
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"category_id": cat2["id"]})
        assert resp.status_code == 200, resp.text
        assert resp.json()["category_id"] == cat2["id"]
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


# ── In-use category delete guard ────────────────────────────────────────────


async def test_category_delete_409_when_in_use(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids, wf_ids = [], []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Used-{suffix}"}).json()
        cat_ids.append(cat["id"])
        wf = client.post("/api/admin/workflows",
                         json={"name": f"wf-used-{suffix}",
                               "yaml_content": _wf_yaml(f"wf-used-{suffix}"),
                               "category_id": cat["id"]}).json()
        wf_ids.append(wf["id"])

        # In use → 409
        resp = client.delete(f"/api/admin/workflow-categories/{cat['id']}")
        assert resp.status_code == 409, resp.text

        # Clearing the category is also rejected — the link can only end by
        # deleting the workflow itself.
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"category_id": None})
        assert resp.status_code == 400, resp.text

        # Delete the workflow (204), then the category deletes (204)
        resp = client.delete(f"/api/admin/workflows/{wf['id']}")
        assert resp.status_code == 204, resp.text
        wf_ids.remove(wf["id"])
        resp = client.delete(f"/api/admin/workflow-categories/{cat['id']}")
        assert resp.status_code == 204, resp.text
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


# ── Clone fidelity (admin + PM copy-to-project) ─────────────────────────────


async def test_copy_to_project_preserves_category(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids, wf_ids = [], []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Clone-{suffix}"}).json()
        cat_ids.append(cat["id"])

        # Platform template with the category (admin copy source)
        source = client.post("/api/admin/workflows",
                             json={"name": f"wf-src-{suffix}",
                                   "yaml_content": _wf_yaml(f"wf-src-{suffix}"),
                                   "category_id": cat["id"]}).json()
        wf_ids.append(source["id"])

        # Admin copy → clone keeps the category
        resp = client.post(
            f"/api/admin/workflows/{source['id']}/copy-to-project",
            json={"project_id": str(world["project"])})
        assert resp.status_code == 201, resp.text
        admin_clone = resp.json()
        assert admin_clone["category_id"] == cat["id"]
        assert admin_clone["category_name"] == f"Clone-{suffix}"

        # Second platform template for the PM endpoint
        source2 = client.post("/api/admin/workflows",
                              json={"name": f"wf-src2-{suffix}",
                                    "yaml_content": _wf_yaml(f"wf-src2-{suffix}"),
                                    "category_id": cat["id"]}).json()
        wf_ids.append(source2["id"])

        # PM copy (dev user has project_manager membership) → same category
        resp = client.post(
            f"/api/workflows/{source2['id']}/copy-to-project",
            json={"project_id": str(world["project"])})
        assert resp.status_code == 201, resp.text
        pm_clone = resp.json()
        assert pm_clone["category_id"] == cat["id"]

        # Persisted on the clone rows, not just echoed
        async with _sm() as session:
            clone = await session.get(Workflow, pm_clone["id"])
            assert clone.workflow_category_id is not None
            assert str(clone.workflow_category_id) == cat["id"]
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


# ── Description persistence ──────────────────────────────────────────────────


async def test_workflow_description_persistence(client):
    """description survives create, admin PATCH, PM PATCH, and both clones."""
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids, wf_ids = [], []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Desc-{suffix}"}).json()
        cat_ids.append(cat["id"])

        # Admin create echoes + persists the description
        resp = client.post("/api/admin/workflows",
                           json={"name": f"wf-desc-{suffix}",
                                 "yaml_content": _wf_yaml(f"wf-desc-{suffix}"),
                                 "category_id": cat["id"],
                                 "description": "Builds the story end-to-end"})
        assert resp.status_code == 201, resp.text
        wf = resp.json()
        wf_ids.append(wf["id"])
        assert wf["description"] == "Builds the story end-to-end"

        # Listed in the admin mirror + single GET
        listed = client.get("/api/admin/workflows").json()
        assert next(w for w in listed if w["id"] == wf["id"])["description"] \
            == "Builds the story end-to-end"
        assert client.get(f"/api/admin/workflows/{wf['id']}").json()["description"] \
            == "Builds the story end-to-end"

        # Admin PATCH updates; an absent key leaves it alone
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"is_active": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Builds the story end-to-end"
        resp = client.patch(f"/api/admin/workflows/{wf['id']}",
                            json={"description": "Admin-edited description"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "Admin-edited description"

        # Admin copy-to-project keeps the description on the clone
        resp = client.post(
            f"/api/admin/workflows/{wf['id']}/copy-to-project",
            json={"project_id": str(world["project"])})
        assert resp.status_code == 201, resp.text
        admin_clone = resp.json()
        assert admin_clone["description"] == "Admin-edited description"

        # PM PATCH on the clone (dev user is PM of the project)
        resp = client.patch(f"/api/workflows/{admin_clone['id']}",
                            json={"description": "PM-edited description"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "PM-edited description"
        # Persisted on the row, not just echoed
        async with _sm() as session:
            clone = await session.get(Workflow, admin_clone["id"])
            assert clone.description == "PM-edited description"

        # PM copy-to-project keeps the description too — from a second source
        # (the project already holds a workflow named 'wf-desc-{suffix}', and
        # project-scoped names are unique per version).
        source2 = client.post("/api/admin/workflows",
                              json={"name": f"wf-desc2-{suffix}",
                                    "yaml_content": _wf_yaml(f"wf-desc2-{suffix}"),
                                    "category_id": cat["id"],
                                    "description": "Second source description"})
        assert source2.status_code == 201, source2.text
        wf_ids.append(source2.json()["id"])
        resp = client.post(
            f"/api/workflows/{source2.json()['id']}/copy-to-project",
            json={"project_id": str(world["project"])})
        assert resp.status_code == 201, resp.text
        pm_clone = resp.json()
        assert pm_clone["description"] == "Second source description"

        # Create WITHOUT description → empty string, not null
        resp = client.post("/api/admin/workflows",
                           json={"name": f"wf-nodesc-{suffix}",
                                 "yaml_content": _wf_yaml(f"wf-nodesc-{suffix}"),
                                 "category_id": cat["id"]})
        assert resp.status_code == 201, resp.text
        wf_ids.append(resp.json()["id"])
        assert resp.json()["description"] == ""
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


# ── Refdata access for non-admins ───────────────────────────────────────────


async def test_refdata_categories_for_non_admin(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids = []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Ref-{suffix}"}).json()
        cat_ids.append(cat["id"])

        # Demote the dev user — refdata needs only an enabled account.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "USER"
            await session.commit()

        resp = client.get("/api/workflow-categories")
        assert resp.status_code == 200, resp.text
        names = [c["name"] for c in resp.json()]
        assert f"Ref-{suffix}" in names

        # And admin-only CRUD now 403s for the same user.
        resp = client.post("/api/admin/workflow-categories",
                           json={"name": f"Ref2-{suffix}"})
        assert resp.status_code == 403, resp.text

        # Restore ADMIN so _cleanup's role restore is accurate.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "ADMIN"
            await session.commit()
    finally:
        await _cleanup(world, category_ids=cat_ids)
