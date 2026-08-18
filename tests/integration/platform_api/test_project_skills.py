"""Integration — project-scoped skills: clone-on-map, PM-gated CRUD, refdata union.

Clone-on-map: copying a platform workflow to a project clones every skill its
steps reference as project-scoped rows (skipping names the project already has —
PM edits win — and names with no platform template). The PM router edits those
rows; the refdata union feeds the workflow editor's skill dropdown.

Loop scoping: same pattern as test_create_run.py — a dedicated engine on the
test's own loop, the platform app on TestClient's portal loop.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bheembhai.models.project import Project
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Workflow

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
    """Platform workflow + skills, PM project, developer project, lonely project.

    All names are suffixed so platform rows never collide with other suites or
    the seeded catalog (the partial unique index is on (name) for platform rows).
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

        pm_project = Project(name=f"pskills-pm-{suffix}", owner_id=user.id)
        dev_project = Project(name=f"pskills-dev-{suffix}", owner_id=user.id)
        lonely_project = Project(name=f"pskills-lonely-{suffix}", owner_id=user.id)
        session.add_all([pm_project, dev_project, lonely_project])
        await session.flush()

        session.add(Membership(user_id=user.id, project_id=pm_project.id,
                               role="project_manager"))
        session.add(Membership(user_id=user.id, project_id=dev_project.id,
                               role="developer"))

        story_name = f"story-design-{suffix}"
        test_name = f"test-creator-{suffix}"
        missing_name = f"missing-skill-{suffix}"

        platform_story = Skill(name=story_name, description="platform story",
                               model="high")
        platform_test = Skill(name=test_name, description="platform tests",
                              model="medium")
        session.add_all([platform_story, platform_test])
        await session.flush()
        session.add_all([
            SkillFile(skill_id=platform_story.id, path="SKILL.md",
                      content="# platform story skill"),
            SkillFile(skill_id=platform_story.id, path="references/context.md",
                      content="platform context"),
            SkillFile(skill_id=platform_test.id, path="SKILL.md",
                      content="# platform test skill"),
        ])

        workflow_yaml = f"""
workflow: clone-test-{suffix}
version: 1
start: story-design
steps:
  - id: story-design
    skill: {story_name}
    model: high
    "on":
      completed: test-creator
  - id: test-creator
    skill: {test_name}
    model: medium
    "on":
      completed: DONE
  - id: missing-step
    skill: {missing_name}
    model: low
    "on":
      completed: DONE
"""
        platform_workflow = Workflow(
            name=f"clone-wf-{suffix}", version=1,
            yaml_content=workflow_yaml, project_id=None,
        )
        session.add(platform_workflow)
        await session.flush()

        await session.commit()
        world = {
            "pm_project": pm_project.id, "dev_project": dev_project.id,
            "lonely_project": lonely_project.id,
            "platform_workflow": platform_workflow.id,
            "story_name": story_name, "test_name": test_name,
            "missing_name": missing_name,
            "platform_story": platform_story.id,
            "platform_test": platform_test.id,
        }
    return world


async def _cleanup(world: dict) -> None:
    """Delete rows in FK-safe order; skills first (cascade removes files)."""
    async with _sm() as session:
        project_skill_ids = (
            await session.execute(
                select(Skill.id).where(
                    Skill.project_id.in_(
                        [world["pm_project"], world["dev_project"],
                         world["lonely_project"]])
                )
            )
        ).scalars().all()
        for skill_id in list(project_skill_ids) + \
                [world["platform_story"], world["platform_test"]]:
            await session.execute(delete(Skill).where(Skill.id == skill_id))
        # Workflow clones created by copy-to-project land on pm/dev projects.
        await session.execute(delete(Workflow).where(
            Workflow.project_id.in_(
                [world["pm_project"], world["dev_project"],
                 world["lonely_project"]])))
        await session.execute(delete(Workflow).where(
            Workflow.id == world["platform_workflow"]))
        for project_id in (world["pm_project"], world["dev_project"],
                           world["lonely_project"]):
            await session.execute(delete(Membership).where(
                Membership.project_id == project_id))
            await session.execute(delete(Project).where(Project.id == project_id))
        await session.commit()


async def _project_skills(world: dict, project_id) -> list[Skill]:
    async with _sm() as session:
        rows = (await session.execute(
            select(Skill)
            .where(Skill.project_id == project_id)
            .order_by(Skill.name))).scalars().all()
        return list(rows)


async def _skill_files(skill_id) -> dict[str, str]:
    async with _sm() as session:
        rows = (await session.execute(
            select(SkillFile).where(SkillFile.skill_id == skill_id))).scalars().all()
        return {f.path: f.content for f in rows}


def _skill_by_name(skills: list[Skill], name: str) -> Skill | None:
    return next((s for s in skills if s.name == name), None)


# ── Clone-on-map ─────────────────────────────────────────────────────────────


async def test_copy_clones_referenced_skills_and_skips_missing(client):
    world = await _make_world()
    try:
        resp = client.post(
            f"/api/workflows/{world['platform_workflow']}/copy-to-project",
            json={"project_id": str(world["pm_project"])})
        assert resp.status_code == 201, resp.text
        clone_id = resp.json()["id"]

        skills = await _project_skills(world, world["pm_project"])
        assert {s.name for s in skills} == {world["story_name"], world["test_name"]}

        # Files came along with the clone — full content, same paths.
        story = _skill_by_name(skills, world["story_name"])
        assert story.project_id == world["pm_project"]
        assert story.description == "platform story"
        assert await _skill_files(story.id) == {
            "SKILL.md": "# platform story skill",
            "references/context.md": "platform context",
        }
        test_skill = _skill_by_name(skills, world["test_name"])
        assert await _skill_files(test_skill.id) == {"SKILL.md": "# platform test skill"}

        # The missing name was skipped, not created.
        assert _skill_by_name(skills, world["missing_name"]) is None

        # Platform templates are untouched.
        async with _sm() as session:
            platform_row = await session.get(Skill, world["platform_story"])
            assert platform_row.project_id is None

        # The cloned workflow row exists (cleaned up by _cleanup).
        async with _sm() as session:
            clone = await session.get(Workflow, clone_id)
            assert clone is not None and clone.project_id == world["pm_project"]
    finally:
        await _cleanup(world)


async def test_copy_skips_names_the_project_already_has_pm_edits_win(client):
    world = await _make_world()
    try:
        # PM edited (created) the skill BEFORE the workflow map.
        pre_resp = client.post(
            f"/api/projects/{world['pm_project']}/skills",
            json={"name": world["story_name"], "description": "PM EDIT WINS",
                  "model": "low"})
        assert pre_resp.status_code == 201, pre_resp.text
        pre_id = pre_resp.json()["id"]
        file_resp = client.post(
            f"/api/projects/{world['pm_project']}/skills/{pre_id}/files",
            json={"path": "SKILL.md", "content": "# pm version"})
        assert file_resp.status_code == 201, file_resp.text

        resp = client.post(
            f"/api/workflows/{world['platform_workflow']}/copy-to-project",
            json={"project_id": str(world["pm_project"])})
        assert resp.status_code == 201, resp.text

        skills = await _project_skills(world, world["pm_project"])
        story = _skill_by_name(skills, world["story_name"])
        # The pre-existing row survived untouched — no overwrite, no file merge.
        assert story.id == uuid.UUID(pre_id)
        assert story.description == "PM EDIT WINS"
        assert story.model == "low"
        assert await _skill_files(story.id) == {"SKILL.md": "# pm version"}
        # The other referenced skill was still cloned.
        assert _skill_by_name(skills, world["test_name"]) is not None
    finally:
        await _cleanup(world)


async def test_copy_to_project_requires_pm_role(client):
    world = await _make_world()
    try:
        resp = client.post(
            f"/api/workflows/{world['platform_workflow']}/copy-to-project",
            json={"project_id": str(world["dev_project"])})
        assert resp.status_code == 403, resp.text
        assert await _project_skills(world, world["dev_project"]) == []
    finally:
        await _cleanup(world)


async def test_admin_copy_to_project_also_clones_skills(client):
    """Regression: the ADMIN copy endpoint (what the admin workflows screen
    posts to) predated clone-on-map and cloned workflow + policies only — the
    project got a workflow with none of its referenced skills. Both copy
    endpoints must share the cloning behavior."""
    world = await _make_world()
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one_or_none()
        assert user is not None
        prev_role = user.platform_role
        user.platform_role = "ADMIN"
        user_id = user.id
        await session.commit()
    try:
        resp = client.post(
            f"/api/admin/workflows/{world['platform_workflow']}/copy-to-project",
            json={"project_id": str(world["pm_project"])})
        assert resp.status_code == 201, resp.text
        skills = await _project_skills(world, world["pm_project"])
        assert {s.name for s in skills} == {world["story_name"], world["test_name"]}
        # Files came along, same as the PM path.
        story = _skill_by_name(skills, world["story_name"])
        assert await _skill_files(story.id) == {
            "SKILL.md": "# platform story skill",
            "references/context.md": "platform context",
        }
    finally:
        async with _sm() as session:
            user = await session.get(User, user_id)
            if user is not None:
                user.platform_role = prev_role
            await session.commit()
        await _cleanup(world)


# ── PM-gated CRUD ────────────────────────────────────────────────────────────


async def test_project_skill_crud_and_file_editing(client):
    world = await _make_world()
    try:
        base = f"/api/projects/{world['pm_project']}/skills"
        # Create
        resp = client.post(base, json={"name": "custom-check", "description": "d",
                                       "model": "high"})
        assert resp.status_code == 201, resp.text
        skill = resp.json()
        assert skill["name"] == "custom-check" and skill["files"] == []
        # Duplicate name within the project → 409 (platform name is NOT blocked —
        # a project may shadow any platform skill on purpose).
        dup = client.post(base, json={"name": "custom-check", "description": "d"})
        assert dup.status_code == 409, dup.text
        # List + get
        listed = client.get(base).json()
        assert any(s["id"] == skill["id"] for s in listed)
        got = client.get(f"{base}/{skill['id']}")
        assert got.status_code == 200 and got.json()["name"] == "custom-check"
        # Metadata PATCH; name is read-only (the workflow's reference key).
        patched = client.patch(f"{base}/{skill['id']}",
                               json={"description": "better", "model": "low"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["description"] == "better"
        assert patched.json()["model"] == "low"
        name_patch = client.patch(f"{base}/{skill['id']}", json={"name": "renamed"})
        assert name_patch.status_code == 400
        assert "read-only" in name_patch.text
        # Files: add, edit content, read back, delete.
        f_resp = client.post(f"{base}/{skill['id']}/files",
                             json={"path": "SKILL.md", "content": "v1"})
        assert f_resp.status_code == 201, f_resp.text
        file_id = f_resp.json()["id"]
        dup_file = client.post(f"{base}/{skill['id']}/files",
                               json={"path": "SKILL.md", "content": "again"})
        assert dup_file.status_code == 409
        updated = client.patch(f"{base}/{skill['id']}/files/{file_id}",
                               json={"content": "v2"})
        assert updated.status_code == 200
        assert updated.json()["content"] == "v2"
        fetched = client.get(f"{base}/{skill['id']}/files/{file_id}")
        assert fetched.json()["content"] == "v2"
        deleted = client.delete(f"{base}/{skill['id']}/files/{file_id}")
        assert deleted.status_code == 204
        assert client.get(f"{base}/{skill['id']}/files/{file_id}").status_code == 404
        # Skill delete → gone from the list.
        assert client.delete(f"{base}/{skill['id']}").status_code == 204
        assert all(s["id"] != skill["id"] for s in client.get(base).json())
    finally:
        await _cleanup(world)


async def test_project_router_cannot_reach_platform_rows(client):
    world = await _make_world()
    try:
        base = f"/api/projects/{world['pm_project']}/skills"
        resp = client.get(f"{base}/{world['platform_story']}")
        assert resp.status_code == 404, resp.text
    finally:
        await _cleanup(world)


async def test_project_skills_are_pm_gated(client):
    world = await _make_world()
    try:
        dev_base = f"/api/projects/{world['dev_project']}/skills"
        lonely_base = f"/api/projects/{world['lonely_project']}/skills"
        # Developer membership → 403 on every endpoint shape.
        assert client.get(dev_base).status_code == 403
        assert client.post(dev_base, json={"name": "x", "description": "d"}
                           ).status_code == 403
        assert client.delete(f"{dev_base}/{uuid.uuid4()}").status_code == 403
        # No membership at all → 403.
        assert client.get(lonely_base).status_code == 403
        # Unknown project → 404 (never leaks a 403).
        assert client.get(f"/api/projects/{uuid.uuid4()}/skills").status_code == 404
    finally:
        await _cleanup(world)


# ── refdata union (workflow editor dropdown) ─────────────────────────────────


async def test_skill_names_union_shadows_platform_with_project_row(client):
    world = await _make_world()
    try:
        # A project skill shadowing a platform skill of the same name.
        resp = client.post(
            f"/api/projects/{world['pm_project']}/skills",
            json={"name": world["story_name"], "description": "shadowed",
                  "model": "medium"})
        assert resp.status_code == 201, resp.text
        project_skill_id = resp.json()["id"]

        # No param → platform skills only.
        names = client.get("/api/skills/names").json()
        by_name = {n["name"]: n["id"] for n in names}
        assert by_name[world["story_name"]] == str(world["platform_story"])

        # With project_id → union, project row wins the name deterministically.
        union = client.get(
            f"/api/skills/names?project_id={world['pm_project']}").json()
        by_name = {n["name"]: n["id"] for n in union}
        assert by_name[world["story_name"]] == project_skill_id
        assert by_name[world["test_name"]] == str(world["platform_test"])

        # Non-member of the lonely project → 403.
        lonely = client.get(
            f"/api/skills/names?project_id={world['lonely_project']}")
        assert lonely.status_code == 403
    finally:
        await _cleanup(world)
