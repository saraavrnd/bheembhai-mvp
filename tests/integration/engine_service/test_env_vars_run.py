"""Integration tests — environment-variable resolution + injection end to end.

Run with `pytest -m integration` and the compose stack's postgres on
localhost:5555 (dedicated `bheembhai_test` database, see tests/integration/README).

Covers the engine half of the env-var feature against real Postgres: platform +
project rows merged at run init (project overrides platform), secret refs
resolved fresh from SecureStorage, fail-fast init on an unresolvable secret,
injection into every step's launch env, and the two guardrail tunables
(BB_MAX_STEP_VISITS / BB_MAX_ATTEMPTS) consumed engine-side.
"""

import hashlib
import uuid

import pytest
import pytest_asyncio
from bheembhai.config import DatabaseConfig
from bheembhai.database import (
    close_database,
    get_sessionmaker,
    init_database,
    run_migrations,
)
from bheembhai.env_vars import env_var_ref
from bheembhai.models.environment import EnvironmentVariable
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run, Step, Transition
from bheembhai.models.skill import Skill
from bheembhai.models.user import User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.providers.env_secrets import EnvSecureStorage
from bheembhai.providers.local_storage import LocalStorage
from sqlalchemy import delete, select

from conftest import FakeRuntime
from engine_service.run_init import InitFailure, init_run
from engine_service.state_machine import drive_run
from engine_service.workflow import WorkflowSpec, resolve_model_tier

# One event loop for the whole module: the session-scoped async engine and its
# pool must never straddle pytest-asyncio's default function-scoped loops.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# ── Workflow/policy fixtures ────────────────────────────────────────────

WF_THREE_STEPS = """
workflow: story-delivery
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: test-creator
  - id: test-creator
    skill: test-creator
    model: low
    "on":
      completed: implement
  - id: implement
    skill: implement
    model: medium
    "on":
      completed: DONE
"""

WF_SELF_LOOP = """
workflow: self-loop
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: DONE
      changes_requested: story-design
"""

POLICY_FAST = "policy: fast\n"


# ── DB fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="session", autouse=True, loop_scope="session")
async def _engine_db():
    """Point the global DB module at the dedicated test database."""
    init_database(DatabaseConfig(url=TEST_DB_URL))
    await run_migrations()
    yield
    await close_database()


@pytest_asyncio.fixture(loop_scope="session")
async def session():
    sm = get_sessionmaker()
    assert sm is not None, "database not initialised"
    created: list = []     # (model_class, id) — deleted at teardown
    async with sm() as s:
        yield s, created
        await s.rollback()
    # Teardown: delete in reverse creation order (FK-safe).
    async with sm() as s2:
        for model, obj_id in reversed(created):
            await s2.execute(delete(model).where(model.id == obj_id))
        await s2.commit()


@pytest_asyncio.fixture(loop_scope="session")
async def secure_storage():
    return EnvSecureStorage()


@pytest.fixture
def config(app_config):
    return app_config


async def _skill(session, name):
    res = await session.execute(select(Skill).where(Skill.name == name))
    return res.scalar_one_or_none()


def _bundle_pin(name: str) -> tuple[str, str]:
    sha = hashlib.sha256(name.encode()).hexdigest()
    return f"skills/{name}/{sha}.tar.gz", sha


def _store(tmp_path):
    return LocalStorage(str(tmp_path / "artifacts"))


async def make_world(session, created, secure_storage, *,
                     wf_yaml=WF_THREE_STEPS, pol_yaml=POLICY_FAST,
                     env_vars=None):
    """Insert a run world (user/project/integrations/workflow/policy/skills/
    run/step rows) plus optional EnvironmentVariable rows.

    `env_vars` is a list of dicts: {name, scope ("platform"|"project"),
    value_type ("plain"|"secret"), value}. Secret values are written to
    SecureStorage under the standard ref — omit the storage write afterwards
    to model an unresolvable secret.
    """
    suffix = uuid.uuid4().hex[:8]
    user = User(external_id=f"ext-{suffix}", auth_provider="test",
                email=f"t-{suffix}@test.co", display_name="Tester")
    session.add(user)
    await session.flush()
    created.append((User, user.id))

    project = Project(name=f"test-proj-{suffix}", owner_id=user.id)
    session.add(project)
    await session.flush()
    created.append((Project, project.id))

    gh = ProjectIntegration(project_id=project.id, type="github",
                            label=f"github-{suffix}",
                            credential_ref=f"gh-ref-{suffix}",
                            config={"url": "https://github.com",
                                    "repository": "acme/demo", "base_branch": "main"})
    vendor = ProjectIntegration(project_id=project.id, type="claude",
                                label=f"claude-{suffix}",
                                credential_ref=f"vendor-ref-{suffix}",
                                config={"model_high": "claude-A",
                                        "model_medium": "claude-B",
                                        "model_low": "claude-C"})
    session.add_all([gh, vendor])
    await session.flush()
    created.extend((ProjectIntegration, r.id) for r in (gh, vendor))

    await secure_storage.put(gh.credential_ref, "ghp_abcd1234")
    await secure_storage.put(vendor.credential_ref, "sk-ant-secret")

    # Skills are catalog rows — get-or-create (unique on name), never deleted.
    wf_spec = WorkflowSpec.load_yaml(wf_yaml)
    bundle_pins: dict[str, tuple[str, str]] = {}
    for sid, spec in wf_spec.steps.items():
        skill = spec.get("skill", sid)
        row = await _skill(session, skill)
        if row is None:
            row = Skill(name=skill, description=f"test skill {skill}",
                        model="medium")
            session.add(row)
        if not row.s3_key:
            row.s3_key, row.sha256 = _bundle_pin(skill)
        bundle_pins[skill] = (row.s3_key, row.sha256)
        await session.flush()

    workflow = Workflow(project_id=project.id, name=f"wf-{suffix}",
                        yaml_content=wf_yaml, is_active=True)
    session.add(workflow)
    await session.flush()
    created.append((Workflow, workflow.id))

    policy = Policy(project_id=project.id, workflow_id=workflow.id,
                    name=f"pol-{suffix}", yaml_content=pol_yaml, is_active=True)
    session.add(policy)
    await session.flush()
    created.append((Policy, policy.id))

    run = Run(project_id=project.id, workflow_id=workflow.id, policy_id=policy.id,
              story_id="LNPRTL-101", source_branch="main",
              run_branch=f"feat/lnprtl-101/{suffix}",
              github_integration_id=gh.id,
              ai_vendor_integration_id=vendor.id, state="pending",
              started_by_user_id=user.id)
    session.add(run)
    await session.flush()
    created.append((Run, run.id))

    for sid, spec in wf_spec.steps.items():
        skill = spec.get("skill", sid)
        row = Step(run_id=run.id, step_id=sid, skill=skill,
                   model_requested=resolve_model_tier(spec.get("model"), vendor.config))
        row.skill_s3_key, row.skill_sha256 = bundle_pins.get(skill, (None, None))
        session.add(row)

    # ── Environment variable rows (feature under test) ──
    for spec in env_vars or []:
        is_platform = spec["scope"] == "platform"
        row = EnvironmentVariable(
            project_id=None if is_platform else project.id,
            scope=spec["scope"], name=spec["name"],
            value_type=spec["value_type"],
            value=spec["value"] if spec["value_type"] == "plain" else None,
            credential_ref=(env_var_ref(None if is_platform else project.id,
                                        spec["name"])
                            if spec["value_type"] == "secret" else None),
        )
        session.add(row)
        await session.flush()
        created.append((EnvironmentVariable, row.id))
        if spec["value_type"] == "secret":
            await secure_storage.put(row.credential_ref, spec["value"])

    await session.commit()
    return {"run": run, "workflow": workflow, "policy": policy}


def start_item(run):
    return WorkQueueItem(run_id=run.id, action="start", payload={})


async def get_run(session, run_id):
    return await session.get(Run, run_id)


def envs_for(rt, step_id):
    return [env for sid, env in rt.envs if sid == step_id]


# ── The feature ─────────────────────────────────────────────────────────

async def test_env_vars_exported_with_project_override(session, secure_storage,
                                                       config, tmp_path):
    """Platform + project rows merge at init (project wins on name clash),
    secret refs resolve to real values, and every step's launch env carries
    the merged set."""
    s, created = session
    world = await make_world(s, created, secure_storage, env_vars=[
        {"scope": "platform", "name": "PLAT_PLAIN", "value_type": "plain",
         "value": "plat-val"},
        {"scope": "platform", "name": "PLAT_SECRET", "value_type": "secret",
         "value": "plat-secret-value"},
        # project override of the platform plain var — must win
        {"scope": "project", "name": "PLAT_PLAIN", "value_type": "plain",
         "value": "proj-override"},
        {"scope": "project", "name": "PROJ_SECRET", "value_type": "secret",
         "value": "proj-secret-value"},
    ])
    store = _store(tmp_path)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"],
                      "implement": ["ok"]}, store=store)

    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    store=store)

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    assert [c[0] for c in rt.calls] == ["story-design", "test-creator", "implement"]

    for step_id in ("story-design", "test-creator", "implement"):
        envs = envs_for(rt, step_id)
        assert len(envs) == 1
        env = envs[0]
        assert env["PLAT_PLAIN"] == "proj-override"          # project override wins
        assert env["PLAT_SECRET"] == "plat-secret-value"     # platform secret resolved
        assert env["PROJ_SECRET"] == "proj-secret-value"     # project secret resolved
        # engine-owned keys untouched by the feature
        assert env["GH_TOKEN"] == "ghp_abcd1234"
        assert env["STEP_ID"] == step_id


async def test_unresolvable_secret_fails_init_before_launch(session, secure_storage,
                                                            config, tmp_path):
    """A secret row whose ref has nothing behind it aborts init with a
    classified InitFailure — zero container launches (fail-fast, ADR-013).
    The worker records the run-level failure from this exception (covered by
    the generic init-failure test in test_state_machine.py)."""
    s, created = session
    world = await make_world(s, created, secure_storage, env_vars=[
        {"scope": "project", "name": "BROKEN_SECRET", "value_type": "secret",
         "value": "will-never-be-stored"},
    ])
    # Model an unresolvable secret: the row exists but SecureStorage no longer
    # holds the value under its ref.
    row = (await s.execute(select(EnvironmentVariable).where(
        EnvironmentVariable.project_id == world["run"].project_id))).scalar_one()
    await secure_storage.delete(row.credential_ref)

    store = _store(tmp_path)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"],
                      "implement": ["ok"]}, store=store)

    with pytest.raises(InitFailure) as excinfo:
        await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                        store=store)

    assert excinfo.value.kind == "failed_execution"
    assert "BROKEN_SECRET" in excinfo.value.reason
    assert rt.calls == []                                   # nothing launched
    run = await get_run(s, world["run"].id)
    assert run.state == "pending"                           # init never committed


async def test_visit_cap_tunable_via_env_var(session, secure_storage, config,
                                             tmp_path):
    """BB_MAX_STEP_VISITS from a project var is consumed engine-side: a
    self-looping workflow with cap 1 halts after exactly one launch (the
    engine default is 3)."""
    s, created = session
    world = await make_world(s, created, secure_storage,
                             wf_yaml=WF_SELF_LOOP, env_vars=[
                                 {"scope": "project", "name": "BB_MAX_STEP_VISITS",
                                  "value_type": "plain", "value": "1"},
                             ])
    store = _store(tmp_path)
    rt = FakeRuntime({"story-design": ["changes"]}, store=store)

    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    store=store)

    run = await get_run(s, world["run"].id)
    assert run.state == "failed"
    assert rt.calls == [("story-design", 1)]                # cap 1, not the default 3
    env = envs_for(rt, "story-design")[0]
    assert env["BB_MAX_STEP_VISITS"] == "1"                 # exported to the container too
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "failed")
        .order_by(Transition.id.desc()).limit(1))
    assert "runaway loop" in res.scalar_one().reason


async def test_attempt_cap_tunable_via_env_var(session, secure_storage, config,
                                               tmp_path):
    """BB_MAX_ATTEMPTS=1 removes the transient retry: a crash on attempt 1
    fails the run instead of relaunching (engine default max_attempts=2)."""
    s, created = session
    world = await make_world(s, created, secure_storage,
                             pol_yaml=POLICY_FAST, env_vars=[
                                 {"scope": "project", "name": "BB_MAX_ATTEMPTS",
                                  "value_type": "plain", "value": "1"},
                             ])
    store = _store(tmp_path)
    rt = FakeRuntime({"story-design": ["crash", "ok"]}, store=store)

    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    store=store)

    run = await get_run(s, world["run"].id)
    assert run.state == "failed"
    assert rt.calls == [("story-design", 1)]                # no second attempt
    env = envs_for(rt, "story-design")[0]
    assert env["BB_MAX_ATTEMPTS"] == "1"


async def test_no_env_var_rows_run_unchanged(session, secure_storage, config,
                                             tmp_path):
    """Regression: with zero configured variables the run behaves exactly as
    before the feature — engine defaults apply, no user keys leak in."""
    s, created = session
    world = await make_world(s, created, secure_storage)
    store = _store(tmp_path)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"],
                      "implement": ["ok"]}, store=store)

    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    store=store)

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    assert len(rt.calls) == 3
    for step_id in ("story-design", "test-creator", "implement"):
        env = envs_for(rt, step_id)[0]
        assert "PLAT_PLAIN" not in env
        assert "BB_MAX_STEP_VISITS" not in env


async def test_init_context_carries_resolved_env_vars(session, secure_storage,
                                                      config):
    """Direct init_run call: the resolved dict rides InitContext (the seam the
    state machine reads) — platform + project merged, project winning."""
    s, created = session
    world = await make_world(s, created, secure_storage, env_vars=[
        {"scope": "platform", "name": "SHARED", "value_type": "plain", "value": "plat"},
        {"scope": "project", "name": "SHARED", "value_type": "plain", "value": "proj"},
        {"scope": "project", "name": "ONLY_PROJ", "value_type": "plain", "value": "p"},
    ])

    ctx = await init_run(s, world["run"].id, config, secure_storage)

    assert ctx.env_vars == {"SHARED": "proj", "ONLY_PROJ": "p"}
