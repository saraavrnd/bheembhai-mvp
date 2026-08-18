"""Integration — admin project deletion cascades cleanly (regression).

Three reported/latent bugs covered:

1. DELETE /api/admin/projects/{id} returned 500 with NotNullViolation on
   project_integrations.project_id: the ORM emulated the cascade by NULLing
   NOT NULL child FKs before the DB constraint could cascade. Fix:
   ``passive_deletes=True`` on the Project relationships (models/project.py)
   so a single DELETE FROM projects lets the DB constraints do the work.
2. skills.project_id was SET NULL — deleting a project whose skill shadows a
   platform skill of the same name re-parented the row into platform scope
   and collided with uq_skills_platform_name. Fix (e6f7a8b9c0d1): CASCADE.
3. workflows.project_id (and therefore policies) was SET NULL — copy-to-project
   clones workflows into project scope with the SAME (name, version) as the
   platform template, so the SET NULL re-parented the copy and collided with
   uq_workflow_platform_name_ver. Fix (e7f8a9b0c1d2): both FKs CASCADE —
   project copies die with their project, platform templates are untouched.

Expected post-delete state: memberships/integrations/runs (and everything
under a run)/project skills/project workflows/project policies gone;
platform rows untouched.

Loop scoping: same pattern as test_create_run.py — a dedicated engine on the
test's own loop, the platform app on TestClient's portal loop. The app's
lifespan runs alembic upgrade head on the test DB before any request.
"""

import uuid

import pytest
import pytest_asyncio
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run, Step, Transition
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import Membership, User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
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
    """A fully-populated project: membership, integration, project workflow +
    policy (same name/version as platform counterparts — the copy-to-project
    collision case), a paused run with steps/transitions/queue, and a project
    skill shadowing a platform skill (the skills-index collision case).

    The dev user's platform_role is promoted to ADMIN for the endpoint's
    require_admin gate and restored in _cleanup.
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

        project = Project(name=f"delproj-{suffix}", owner_id=user.id)
        session.add(project)
        await session.flush()

        session.add(Membership(user_id=user.id, project_id=project.id,
                               role="project_manager"))
        session.add(ProjectIntegration(
            project_id=project.id, type="deepseek", label="deepseek-default",
            credential_ref="cred-ref", config={},
        ))

        # Workflows + policies: platform template pair and project copies with
        # the SAME (name, version) — the exact copy-to-project shadow shape.
        wf_name = f"story-delivery-{suffix}"
        pol_name = f"policy-strict-{suffix}"
        platform_workflow = Workflow(
            name=wf_name, version=1, yaml_content=f"workflow: {wf_name}\nversion: 1\n",
            project_id=None,
        )
        session.add(platform_workflow)
        await session.flush()
        platform_policy = Policy(
            name=pol_name, version=1, workflow_id=platform_workflow.id,
            yaml_content=f"policy: {pol_name}\n", project_id=None,
        )
        session.add(platform_policy)

        project_workflow = Workflow(
            name=wf_name, version=1, yaml_content=f"workflow: {wf_name}\nversion: 1\n",
            project_id=project.id,
        )
        session.add(project_workflow)
        await session.flush()
        project_policy = Policy(
            name=pol_name, version=1, workflow_id=project_workflow.id,
            yaml_content=f"policy: {pol_name}\n", project_id=project.id,
        )
        session.add(project_policy)
        await session.flush()

        skill_name = f"shadowed-skill-{suffix}"
        platform_skill = Skill(name=skill_name, description="platform copy",
                               model="medium")
        session.add(platform_skill)
        await session.flush()
        session.add(SkillFile(skill_id=platform_skill.id, path="SKILL.md",
                              content="# platform"))

        project_skill = Skill(name=skill_name, description="project edit",
                              model="medium", project_id=project.id)
        session.add(project_skill)
        await session.flush()
        session.add(SkillFile(skill_id=project_skill.id, path="SKILL.md",
                              content="# project edit"))

        run = Run(project_id=project.id, workflow_id=project_workflow.id,
                  policy_id=project_policy.id, source_branch="main",
                  state="paused", current_step="story-design")
        session.add(run)
        await session.flush()
        session.add(Step(run_id=run.id, step_id="story-design",
                         skill=skill_name, exec_state="completed"))
        session.add(Transition(run_id=run.id, step_id="story-design",
                               attempt_no=1, from_state="running",
                               to_state="awaiting_approval",
                               payload={"gate": "card"}, ts=1.0))
        session.add(WorkQueueItem(run_id=run.id, action="continue",
                                  payload={"action": "approve"},
                                  state="pending"))

        await session.commit()
        return {
            "project": project.id, "run": run.id,
            "platform_workflow": platform_workflow.id,
            "platform_policy": platform_policy.id,
            "project_workflow": project_workflow.id,
            "project_policy": project_policy.id,
            "platform_skill": platform_skill.id,
            "project_skill": project_skill.id,
            "user": user.id, "prev_role": prev_role,
        }


async def _cleanup(world: dict) -> None:
    """Remove whatever survives; restore the dev user's role.

    Core ``delete(Project)`` relies purely on the DB cascade chain — this also
    cleans up completely when the test itself failed before the endpoint ran.
    """
    async with _sm() as session:
        await session.execute(delete(Project).where(
            Project.id == world["project"]))
        # Remaining platform rows, in FK-safe order (policies → workflows).
        await session.execute(delete(Policy).where(
            Policy.id.in_([world["platform_policy"], world["project_policy"]])))
        await session.execute(delete(Workflow).where(
            Workflow.id.in_([world["platform_workflow"],
                             world["project_workflow"]])))
        # SkillFile rows cascade from skills.
        await session.execute(delete(Skill).where(
            Skill.id.in_([world["platform_skill"], world["project_skill"]])))
        user = await session.get(User, world["user"])
        if user is not None:
            user.platform_role = world["prev_role"]
        await session.commit()


async def test_admin_project_delete_cascades_full_world(client):
    world = await _make_world()
    try:
        resp = client.delete(f"/api/admin/projects/{world['project']}")
        assert resp.status_code == 204, resp.text

        async with _sm() as session:
            # The project and everything CASCADE-linked is gone.
            assert await session.get(Project, world["project"]) is None
            assert await session.get(Run, world["run"]) is None
            assert await session.get(Skill, world["project_skill"]) is None
            assert await session.get(Workflow, world["project_workflow"]) is None
            assert await session.get(Policy, world["project_policy"]) is None
            assert (await session.execute(select(Membership).where(
                Membership.project_id == world["project"]))).scalars().first() is None
            assert (await session.execute(select(ProjectIntegration).where(
                ProjectIntegration.project_id == world["project"]))).scalars().first() is None
            assert (await session.execute(select(Step).where(
                Step.run_id == world["run"]))).scalars().first() is None
            assert (await session.execute(select(Transition).where(
                Transition.run_id == world["run"]))).scalars().first() is None
            assert (await session.execute(select(WorkQueueItem).where(
                WorkQueueItem.run_id == world["run"]))).scalars().first() is None

            # Platform template rows are untouched — no shadow collisions.
            platform_workflow = await session.get(Workflow, world["platform_workflow"])
            assert platform_workflow is not None
            assert platform_workflow.project_id is None
            platform_policy = await session.get(Policy, world["platform_policy"])
            assert platform_policy is not None
            assert platform_policy.project_id is None
            assert platform_policy.workflow_id == platform_workflow.id
            platform_skill = await session.get(Skill, world["platform_skill"])
            assert platform_skill is not None
            assert platform_skill.project_id is None
            files = (await session.execute(select(SkillFile).where(
                SkillFile.skill_id == platform_skill.id))).scalars().all()
            assert len(files) == 1

        # The endpoint is idempotent-safe about unknown ids: 404, not 500.
        resp2 = client.delete(f"/api/admin/projects/{world['project']}")
        assert resp2.status_code == 404, resp2.text
    finally:
        await _cleanup(world)
