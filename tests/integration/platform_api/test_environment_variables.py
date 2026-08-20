"""Integration — environment-variable API: project-scoped CRUD + admin
platform scope.

Covered: plain roundtrip, secret ref storage + masking, rotation, secret
rename ref migration, immutable value_type (422), duplicate 409, reserved/
invalid name 400, tunable validation, permission gates (member reads, manager
writes, admin for platform scope), the merged GET view with override flags,
and cross-project isolation.

Loop scoping: same pattern as test_create_run.py — a dedicated engine on the
test's own loop, the platform app on TestClient's portal loop.
"""

import uuid

import pytest
import pytest_asyncio
from bheembhai.env_vars import env_var_ref
from bheembhai.models.environment import EnvironmentVariable
from bheembhai.models.project import Project
from bheembhai.models.user import Membership, User
from bheembhai.providers.env_secrets import EnvSecureStorage
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
    """PM project + developer project + a second PM project (isolation), plus
    platform-scope rows inserted directly (the admin API is exercised in its
    own tests). The dev user's platform_role is pinned to USER as a
    deterministic baseline and restored in _cleanup."""
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

        pm_project = Project(name=f"envvars-pm-{suffix}", owner_id=user.id)
        dev_project = Project(name=f"envvars-dev-{suffix}", owner_id=user.id)
        other_project = Project(name=f"envvars-other-{suffix}", owner_id=user.id)
        session.add_all([pm_project, dev_project, other_project])
        await session.flush()

        session.add(Membership(user_id=user.id, project_id=pm_project.id,
                               role="project_manager"))
        session.add(Membership(user_id=user.id, project_id=dev_project.id,
                               role="developer"))
        session.add(Membership(user_id=user.id, project_id=other_project.id,
                               role="project_manager"))

        # Platform-scope rows (direct insert — what the admin page manages).
        plat_plain = EnvironmentVariable(
            project_id=None, scope="platform",
            name=f"PLAT_PLAIN_{suffix}", value_type="plain", value="plat-val")
        plat_only = EnvironmentVariable(
            project_id=None, scope="platform",
            name=f"PLAT_ONLY_{suffix}", value_type="plain", value="only")
        session.add_all([plat_plain, plat_only])

        await session.commit()
        return {
            "pm_project": pm_project.id,
            "dev_project": dev_project.id,
            "other_project": other_project.id,
            "user": user.id, "prev_role": prev_role, "suffix": suffix,
            "plat_plain": plat_plain.id,
            "plat_plain_name": plat_plain.name,
            "plat_only": plat_only.id,
            "plat_only_name": plat_only.name,
        }


async def _cleanup(world: dict) -> None:
    """Delete project rows (cascade) + any platform-scope rows created, purge
    stored secrets from the process-global EnvSecureStorage store, restore the
    dev user's role."""
    suffix = world["suffix"]
    async with _sm() as session:
        user = await session.get(User, world["user"])
        if user is not None:
            user.platform_role = world["prev_role"]
        for pid in (world["pm_project"], world["dev_project"], world["other_project"]):
            await session.execute(delete(Project).where(Project.id == pid))
        await session.execute(delete(EnvironmentVariable).where(
            EnvironmentVariable.project_id.is_(None),
            EnvironmentVariable.name.like(f"%_{suffix}%")))
        await session.commit()
    # EnvSecureStorage is a process-global dict — drop any refs this test wrote
    # so leaked secrets can't satisfy a later suite's assertions.
    store = EnvSecureStorage()
    for scope in ("platform", str(world["pm_project"]), str(world["dev_project"]),
                  str(world["other_project"])):
        for name in (f"PLAT_PLAIN_{suffix}", f"PLAT_ONLY_{suffix}",
                     f"PROJ_SECRET_{suffix}", f"RENAME_SECRET_{suffix}",
                     f"ROTATE_SECRET_{suffix}", f"ADMIN_SECRET_{suffix}",
                     f"ADMIN_RENAME_{suffix}"):
            await store.delete(env_var_ref(scope, name))


def _url(project_id, envvar_id=None):
    base = f"/api/projects/{project_id}/environment-variables"
    return f"{base}/{envvar_id}" if envvar_id else base


def _admin_url(envvar_id=None):
    base = "/api/admin/environment-variables"
    return f"{base}/{envvar_id}" if envvar_id else base


# ── Project-scoped CRUD ──────────────────────────────────────────────────

async def test_plain_crud_roundtrip(client):
    world = await _make_world()
    try:
        name = f"PLAIN_{world['suffix']}"
        # Create
        r = client.post(_url(world["pm_project"]), json={
            "name": name, "value_type": "plain", "value": "hello",
            "description": "a plain var"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["name"] == name
        assert body["scope"] == "project"
        assert body["value"] == "hello"
        assert body["value_type"] == "plain"
        assert body["description"] == "a plain var"
        envvar_id = body["id"]

        # List shows it
        r = client.get(_url(world["pm_project"]))
        assert r.status_code == 200, r.text
        listed = [x for x in r.json() if x["name"] == name]
        assert len(listed) == 1 and listed[0]["value"] == "hello"

        # Update value + description
        r = client.patch(_url(world["pm_project"], envvar_id),
                         json={"value": "updated", "description": "new desc"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["value"] == "updated"
        assert body["description"] == "new desc"

        # Delete
        r = client.delete(_url(world["pm_project"], envvar_id))
        assert r.status_code == 204
        r = client.get(_url(world["pm_project"]))
        assert all(x["name"] != name for x in r.json())
    finally:
        await _cleanup(world)


async def test_secret_value_masked_and_stored_under_ref(client):
    world = await _make_world()
    try:
        name = f"PROJ_SECRET_{world['suffix']}"
        r = client.post(_url(world["pm_project"]), json={
            "name": name, "value_type": "secret", "value": "hunter2"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["value"] is None          # never returned
        assert body["has_value"] is True
        envvar_id = body["id"]

        # DB holds the ref, never the raw value
        async with _sm() as session:
            row = await session.get(EnvironmentVariable, uuid.UUID(envvar_id))
            assert row.value is None
            assert row.credential_ref == env_var_ref(world["pm_project"], name)

        # SecureStorage holds the raw value under that ref
        cred = await EnvSecureStorage().get(env_var_ref(world["pm_project"], name))
        assert cred is not None and cred.value == "hunter2"

        # GET list masks it too
        r = client.get(_url(world["pm_project"]))
        listed = [x for x in r.json() if x["name"] == name]
        assert listed[0]["value"] is None and listed[0]["has_value"] is True
    finally:
        await _cleanup(world)


async def test_secret_rotation_replaces_stored_value(client):
    world = await _make_world()
    try:
        name = f"ROTATE_SECRET_{world['suffix']}"
        r = client.post(_url(world["pm_project"]), json={
            "name": name, "value_type": "secret", "value": "v1"})
        envvar_id = r.json()["id"]

        r = client.patch(_url(world["pm_project"], envvar_id), json={"value": "v2"})
        assert r.status_code == 200, r.text

        cred = await EnvSecureStorage().get(env_var_ref(world["pm_project"], name))
        assert cred is not None and cred.value == "v2"
    finally:
        await _cleanup(world)


async def test_rename_secret_migrates_ref(client):
    world = await _make_world()
    try:
        old_name = f"RENAME_SECRET_{world['suffix']}"
        new_name = f"RENAME_SECRET_2_{world['suffix']}"
        r = client.post(_url(world["pm_project"]), json={
            "name": old_name, "value_type": "secret", "value": "secret-val"})
        envvar_id = r.json()["id"]

        r = client.patch(_url(world["pm_project"], envvar_id), json={"name": new_name})
        assert r.status_code == 200, r.text
        assert r.json()["name"] == new_name

        store = EnvSecureStorage()
        assert (await store.get(env_var_ref(world["pm_project"], old_name))) is None
        cred = await store.get(env_var_ref(world["pm_project"], new_name))
        assert cred is not None and cred.value == "secret-val"
    finally:
        await _cleanup(world)


async def test_patch_value_type_rejected_422(client):
    world = await _make_world()
    try:
        name = f"PLAIN_{world['suffix']}"
        envvar_id = client.post(_url(world["pm_project"]), json={
            "name": name, "value_type": "plain", "value": "x"}).json()["id"]

        # value_type is immutable — extra keys are forbidden on PATCH.
        r = client.patch(_url(world["pm_project"], envvar_id),
                         json={"value_type": "secret"})
        assert r.status_code == 422
    finally:
        await _cleanup(world)


async def test_duplicate_name_conflict_409(client):
    world = await _make_world()
    try:
        name = f"DUP_{world['suffix']}"
        payload = {"name": name, "value_type": "plain", "value": "first"}
        assert client.post(_url(world["pm_project"]), json=payload).status_code == 201
        r = client.post(_url(world["pm_project"]), json=payload)
        assert r.status_code == 409
        assert name in r.json()["detail"]
    finally:
        await _cleanup(world)


async def test_reserved_and_invalid_names_rejected(client):
    world = await _make_world()
    try:
        for bad_name in ("GH_TOKEN", "BB_SKILL_URL", "RUN_ID", "1BAD", "has-dash",
                         "BB_RESULT_PUT_URL"):
            r = client.post(_url(world["pm_project"]), json={
                "name": bad_name, "value_type": "plain", "value": "x"})
            assert r.status_code == 400, f"{bad_name}: {r.status_code} {r.text}"
    finally:
        await _cleanup(world)


async def test_tunable_values_validated(client):
    world = await _make_world()
    try:
        for bad in ("abc", "0", "-3"):
            r = client.post(_url(world["pm_project"]), json={
                "name": "BB_MAX_STEP_VISITS", "value_type": "plain", "value": bad})
            assert r.status_code == 400, f"{bad}: {r.status_code}"
        # positive ints are fine — both tunables
        r = client.post(_url(world["pm_project"]), json={
            "name": "BB_MAX_STEP_VISITS", "value_type": "plain", "value": "1"})
        assert r.status_code == 201, r.text
        r = client.post(_url(world["pm_project"]), json={
            "name": "BB_MAX_ATTEMPTS", "value_type": "plain", "value": "7"})
        assert r.status_code == 201, r.text
    finally:
        await _cleanup(world)


async def test_developer_cannot_write_member_can_read(client):
    world = await _make_world()
    try:
        name = f"DEVWRITE_{world['suffix']}"
        r = client.post(_url(world["dev_project"]), json={
            "name": name, "value_type": "plain", "value": "x"})
        assert r.status_code == 403
        r = client.patch(_url(world["dev_project"], "does-not-matter"),
                         json={"value": "x"})
        assert r.status_code in (403, 404)     # permission gate fires first
        r = client.delete(_url(world["dev_project"], "does-not-matter"))
        assert r.status_code in (403, 404)
        # member read is allowed
        r = client.get(_url(world["dev_project"]))
        assert r.status_code == 200
    finally:
        await _cleanup(world)


async def test_merged_view_override_flags_and_isolation(client):
    world = await _make_world()
    try:
        suffix = world["suffix"]
        plat_plain_name = world["plat_plain_name"]
        plat_only_name = world["plat_only_name"]
        # project override of the platform plain var + a project-only var
        r = client.post(_url(world["pm_project"]), json={
            "name": plat_plain_name, "value_type": "plain", "value": "proj-wins"})
        assert r.status_code == 201, r.text
        r = client.post(_url(world["pm_project"]), json={
            "name": f"PROJ_ONLY_{suffix}", "value_type": "plain", "value": "mine"})
        assert r.status_code == 201, r.text

        r = client.get(_url(world["pm_project"]))
        assert r.status_code == 200
        by_source = {(x["name"], x["source"]): x for x in r.json()}
        # platform row: present, marked overridden
        plat_row = by_source[(plat_plain_name, "platform")]
        assert plat_row["overridden"] is True
        # platform-only row: present, not overridden
        assert by_source[(plat_only_name, "platform")]["overridden"] is False
        # project override row: flagged + carries its own value
        proj_row = by_source[(plat_plain_name, "project")]
        assert proj_row["overrides_platform"] is True
        assert proj_row["value"] == "proj-wins"

        # Isolation: the other PM project sees neither project row, and its
        # platform view is NOT marked overridden.
        r = client.get(_url(world["other_project"]))
        assert r.status_code == 200
        other_by_source = {(x["name"], x["source"]): x for x in r.json()}
        assert (f"PROJ_ONLY_{suffix}", "project") not in other_by_source
        assert (plat_plain_name, "project") not in other_by_source
        assert other_by_source[(plat_plain_name, "platform")]["overridden"] is False

        # Project writes never touch platform rows: DELETE the project
        # override, then confirm the platform row is still there.
        assert client.delete(_url(world["pm_project"], proj_row["id"])).status_code == 204
        r = client.get(_url(world["pm_project"]))
        remaining = {(x["name"], x["source"]): x for x in r.json()}
        assert (plat_plain_name, "project") not in remaining
        plat_row = remaining[(plat_plain_name, "platform")]
        assert plat_row["overridden"] is False and plat_row["value"] == "plat-val"
    finally:
        await _cleanup(world)


async def test_delete_removes_stored_secret(client):
    world = await _make_world()
    try:
        name = f"DEL_SECRET_{world['suffix']}"
        envvar_id = client.post(_url(world["pm_project"]), json={
            "name": name, "value_type": "secret", "value": "gone-soon"}).json()["id"]

        assert client.delete(_url(world["pm_project"], envvar_id)).status_code == 204
        assert (await EnvSecureStorage().get(
            env_var_ref(world["pm_project"], name))) is None
    finally:
        await _cleanup(world)


# ── Admin (platform scope) ───────────────────────────────────────────────

async def test_admin_endpoints_require_admin(client):
    world = await _make_world()          # platform_role pinned to USER
    try:
        r = client.get(_admin_url())
        assert r.status_code == 403
        r = client.post(_admin_url(), json={
            "name": f"ADMIN_PLAIN_{world['suffix']}",
            "value_type": "plain", "value": "x"})
        assert r.status_code == 403
    finally:
        await _cleanup(world)


async def test_admin_platform_crud(client):
    world = await _make_world()
    suffix = world["suffix"]
    try:
        # Promote (require_admin gates on the DB row's platform_role).
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "ADMIN"
            await session.commit()

        name = f"ADMIN_PLAIN_{suffix}"
        secret_name = f"ADMIN_SECRET_{suffix}"
        r = client.post(_admin_url(), json={
            "name": name, "value_type": "plain", "value": "plat-val",
            "description": "platform-wide"})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["scope"] == "platform"
        assert body["source"] == "platform"
        plain_id = body["id"]

        r = client.post(_admin_url(), json={
            "name": secret_name, "value_type": "secret", "value": "plat-secret"})
        assert r.status_code == 201, r.text
        secret_id = r.json()["id"]
        assert r.json()["value"] is None
        cred = await EnvSecureStorage().get(env_var_ref(None, secret_name))
        assert cred is not None and cred.value == "plat-secret"

        # List
        r = client.get(_admin_url())
        names = {x["name"] for x in r.json()}
        assert {name, secret_name} <= names

        # Update plain value + description; rename the secret (ref migration)
        r = client.patch(_admin_url(plain_id),
                         json={"value": "plat-val-2", "description": "edited"})
        assert r.status_code == 200
        assert r.json()["value"] == "plat-val-2"

        new_name = f"ADMIN_RENAME_{suffix}"
        r = client.patch(_admin_url(secret_id), json={"name": new_name})
        assert r.status_code == 200
        assert r.json()["name"] == new_name
        store = EnvSecureStorage()
        assert (await store.get(env_var_ref(None, secret_name))) is None
        cred = await store.get(env_var_ref(None, new_name))
        assert cred is not None and cred.value == "plat-secret"

        # Delete both
        assert client.delete(_admin_url(plain_id)).status_code == 204
        assert client.delete(_admin_url(secret_id)).status_code == 204
        r = client.get(_admin_url())
        assert name not in {x["name"] for x in r.json()}
        assert new_name not in {x["name"] for x in r.json()}
        assert (await store.get(env_var_ref(None, new_name))) is None
    finally:
        await _cleanup(world)
