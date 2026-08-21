"""Integration — POST /api/runs resolves the per-run source-branch override.

The run modal gained an editable "Source branch" field (ADR-013 deferred item):
the user's value wins, else the selected GitHub integration's ``base_branch``,
else ``main``. The engine cuts the run branch off the stored value at init.

Loop scoping: same pattern as test_run_timeline.py — a dedicated engine on the
test's own loop, and the platform app on TestClient's portal loop.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run
from bheembhai.models.skill import Skill, SkillFile
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

# Ad-hoc (ADR-016): the platform template the API auto-provisions per project.
ADHOC_YAML = """
workflow: adhoc
version: 1
category: Ad-hoc
start: adhoc
steps:
  - id: adhoc
    skill: adhoc
    model: high
    deadline: 3600
    "on":
      completed: DONE
"""

ADHOC_POLICY_YAML = """
policy: adhoc
version: 1
applies_to: adhoc
gates: {}
"""

MULTI_STEP_YAML = """
workflow: two-step
version: 1
start: first
steps:
  - id: first
    skill: story-design
    model: high
    "on":
      completed: second
  - id: second
    skill: implement
    model: medium
    "on":
      completed: DONE
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

        project = Project(name=f"create-run-proj-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        session.add(Membership(user_id=user.id, project_id=project.id,
                               role="developer"))

        workflow = Workflow(
            name=f"create-run-wf-{suffix}", version=1,
            yaml_content=WORKFLOW_YAML, project_id=project.id,
        )
        session.add(workflow)
        await session.flush()

        policy = Policy(
            name=f"create-run-pol-{suffix}", version=1,
            yaml_content=POLICY_YAML, workflow_id=workflow.id, project_id=project.id,
        )
        session.add(policy)
        await session.flush()

        github = ProjectIntegration(
            project_id=project.id, type="github", label=f"gh-{suffix}",
            credential_ref=f"gh-ref-{suffix}",
            config={"url": "https://github.com", "repository": "acme/demo",
                    "base_branch": "develop"},
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
        "story_id": "LNPRTL-999",
        "github_integration_id": str(world["github_id"]),
        "ai_vendor_integration_id": str(world["ai_id"]),
    }
    body.update(overrides)
    return body


async def _created_run_id(world: dict, story_id: str) -> uuid.UUID:
    async with _sm() as session:
        row = (await session.execute(
            select(Run.id).where(Run.project_id == world["project_id"],
                                 Run.story_id == story_id))).scalar_one()
        return row


async def _ensure_platform_adhoc_template(session) -> dict:
    """Platform-scoped ad-hoc template (workflow + policy + skill), created only
    when missing. Returns the ids plus the rows THIS call created (for cleanup —
    pre-existing seeded rows are never deleted)."""
    created: list[tuple] = []
    wf = (await session.execute(
        select(Workflow).where(Workflow.project_id.is_(None),
                               Workflow.name == "adhoc")
    )).scalars().first()
    if wf is None:
        wf = Workflow(name="adhoc", version=1, yaml_content=ADHOC_YAML,
                      description="Ad-hoc session template (test)", project_id=None)
        session.add(wf)
        await session.flush()
        created.append((Workflow, wf.id))
    pol = (await session.execute(
        select(Policy).where(Policy.workflow_id == wf.id)
    )).scalar_one_or_none()
    if pol is None:
        pol = Policy(name="adhoc", version=1, yaml_content=ADHOC_POLICY_YAML,
                     workflow_id=wf.id, project_id=None, is_active=True)
        session.add(pol)
        await session.flush()
        created.append((Policy, pol.id))
    skill = (await session.execute(
        select(Skill).where(Skill.project_id.is_(None), Skill.name == "adhoc")
    )).scalars().first()
    if skill is None:
        skill = Skill(project_id=None, name="adhoc", description="test adhoc skill")
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md",
                              content="# ad-hoc test skill\n"))
        created.append((Skill, skill.id))
    return {"workflow_id": wf.id, "policy_id": pol.id, "skill_id": skill.id,
            "created": created}


async def test_source_branch_override_wins(client):
    world = await _make_world()
    created = []
    try:
        resp = client.post("/api/runs", json=_create_body(
            world, source_branch="release/2026.08"))
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["source_branch"] == "release/2026.08"
        created.append(await _created_run_id(world, "LNPRTL-999"))
        # The engine must read the stored override at init — pin the row, not
        # just the response.
        async with _sm() as session:
            run = await session.get(Run, created[0])
            assert run.source_branch == "release/2026.08"
            assert run.state == "pending"     # bookkeeper: enqueued, not driven
    finally:
        await _cleanup(world, created)


async def test_source_branch_falls_back_to_integration_base_branch(client):
    world = await _make_world()
    created = []
    try:
        resp = client.post("/api/runs", json=_create_body(world))
        assert resp.status_code == 201, resp.text
        assert resp.json()["source_branch"] == "develop"  # integration's base_branch
        created.append(await _created_run_id(world, "LNPRTL-999"))
    finally:
        await _cleanup(world, created)


async def test_invalid_source_branch_is_422(client):
    world = await _make_world()
    try:
        for bad in ("dev..elop", " main", "has space"):
            resp = client.post("/api/runs", json=_create_body(
                world, story_id=f"LNPRTL-{bad[:6]}", source_branch=bad))
            assert resp.status_code == 422, f"{bad!r} -> {resp.status_code}: {resp.text}"
            assert "source branch" in resp.text.lower()
    finally:
        await _cleanup(world, [])


# ── Ad-hoc sessions (ADR-016) ─────────────────────────────────────────


async def test_adhoc_requires_query(client):
    world = await _make_world()
    try:
        resp = client.post("/api/runs", json=_create_body(
            world, run_kind="adhoc", story_id=None))
        assert resp.status_code == 422, resp.text
        assert "query" in resp.text.lower()
    finally:
        await _cleanup(world, [])


async def test_adhoc_requires_source_branch(client):
    world = await _make_world()
    try:
        resp = client.post("/api/runs", json=_create_body(
            world, run_kind="adhoc", story_id=None, query="do the thing"))
        assert resp.status_code == 422, resp.text
        assert "branch" in resp.text.lower()
    finally:
        await _cleanup(world, [])


async def test_workflow_run_requires_story_id(client):
    """Ad-hoc relaxed story_id; the governed pipeline still demands it."""
    world = await _make_world()
    try:
        resp = client.post("/api/runs", json=_create_body(world, story_id=None))
        assert resp.status_code == 422, resp.text
        assert "story_id" in resp.text.lower()
    finally:
        await _cleanup(world, [])


async def test_adhoc_rejects_multi_step_workflow(client):
    """The 1-step guard: a 2-step workflow would execute the query once per
    step, which is not a session."""
    world = await _make_world()
    wf2_id = None
    try:
        async with _sm() as session:
            wf2 = Workflow(name=f"two-step-{uuid.uuid4().hex[:8]}", version=1,
                           yaml_content=MULTI_STEP_YAML,
                           project_id=world["project_id"])
            session.add(wf2)
            await session.flush()
            wf2_id = wf2.id
            await session.commit()
        resp = client.post("/api/runs", json=_create_body(
            world, workflow_id=str(wf2_id), run_kind="adhoc",
            query="do the thing", source_branch="develop"))
        assert resp.status_code == 400, resp.text
        assert "1-step" in resp.text
    finally:
        async with _sm() as session:
            if wf2_id is not None:
                await session.execute(delete(Workflow).where(Workflow.id == wf2_id))
                await session.commit()
        await _cleanup(world, [])


async def test_adhoc_auto_provisions_platform_template_idempotently(client):
    """Submitting against the platform 'adhoc' template clones workflow +
    policy + skills into the project on first use and reuses the clone after."""
    world = await _make_world()
    created_runs = []
    template = {"created": []}
    try:
        async with _sm() as session:
            template = await _ensure_platform_adhoc_template(session)
            await session.commit()

        body = _create_body(world, workflow_id=str(template["workflow_id"]),
                            policy_id=None, run_kind="adhoc",
                            query="fix the flaky login test",
                            source_branch="develop")
        resp = client.post("/api/runs", json=body)
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["run_kind"] == "adhoc"
        assert data["user_query"] == "fix the flaky login test"
        assert data["story_id"] is None
        assert data["source_branch"] == "develop"
        created_runs.append(uuid.UUID(data["id"]))

        async with _sm() as session:
            run = await session.get(Run, created_runs[0])
            assert run.run_kind == "adhoc"
            assert run.user_query == "fix the flaky login test"
            assert run.story_id is None
            assert run.state == "pending"      # bookkeeper: enqueued, not driven
            clone = (await session.execute(select(Workflow).where(
                Workflow.project_id == world["project_id"],
                Workflow.name == "adhoc"))).scalars().one()
            assert run.workflow_id == clone.id
            assert run.workflow_id != template["workflow_id"]
            pol_clone = (await session.execute(select(Policy).where(
                Policy.workflow_id == clone.id,
                Policy.project_id == world["project_id"]))).scalars().all()
            assert len(pol_clone) == 1
            assert run.policy_id == pol_clone[0].id
            skill_clone = (await session.execute(select(Skill).where(
                Skill.project_id == world["project_id"],
                Skill.name == "adhoc"))).scalar_one_or_none()
            assert skill_clone is not None, "referenced skill cloned with the template"

        # Second submit against the same platform template: reuse the clone.
        resp2 = client.post("/api/runs", json={**body, "query": "second thing"})
        assert resp2.status_code == 201, resp2.text
        created_runs.append(uuid.UUID(resp2.json()["id"]))
        async with _sm() as session:
            clones = (await session.execute(select(Workflow).where(
                Workflow.project_id == world["project_id"],
                Workflow.name == "adhoc"))).scalars().all()
            assert len(clones) == 1
            run2 = await session.get(Run, created_runs[1])
            assert run2.workflow_id == clones[0].id
    finally:
        async with _sm() as session:
            for run_id in created_runs:
                await session.execute(delete(WorkQueueItem).where(
                    WorkQueueItem.run_id == run_id))
                await session.execute(delete(Run).where(Run.id == run_id))
            # Auto-provisioned project clones.
            clone_skill_ids = (await session.execute(select(Skill.id).where(
                Skill.project_id == world["project_id"],
                Skill.name == "adhoc"))).scalars().all()
            for sid in clone_skill_ids:
                await session.execute(delete(SkillFile).where(SkillFile.skill_id == sid))
                await session.execute(delete(Skill).where(Skill.id == sid))
            await session.execute(delete(Policy).where(
                Policy.project_id == world["project_id"], Policy.name == "adhoc"))
            await session.execute(delete(Workflow).where(
                Workflow.project_id == world["project_id"], Workflow.name == "adhoc"))
            # Platform template rows THIS test created (leave seeded rows alone).
            # FK-safe order — Policy.workflow_id has no ondelete, so policies
            # must go before their workflow.
            for model, row_id in template["created"]:
                if model is Skill:
                    await session.execute(delete(SkillFile).where(
                        SkillFile.skill_id == row_id))
                    await session.execute(delete(Skill).where(Skill.id == row_id))
            for model, row_id in template["created"]:
                if model is Policy:
                    await session.execute(delete(Policy).where(Policy.id == row_id))
            for model, row_id in template["created"]:
                if model is Workflow:
                    await session.execute(delete(Workflow).where(Workflow.id == row_id))
            await session.commit()
        await _cleanup(world, [])
