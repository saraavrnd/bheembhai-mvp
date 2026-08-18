"""Integration tests — the run state machine + dispatch against real Postgres.

Run with `pytest -m integration` and the compose stack's postgres on
localhost:5555. These use a dedicated `bheembhai_test` database (created once,
see tests/integration/README if missing) so dev data is never touched.

No Docker containers are launched: FakeRuntime stands in for the Runtime
protocol. What's exercised is the machine's persistence, routing, gates,
dispatch guards, and crash-resume semantics — everything below the runtime seam.
"""

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from bheembhai.config import DatabaseConfig
from bheembhai.database import (
    close_database,
    get_sessionmaker,
    init_database,
    run_migrations,
)
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run, Step, Transition
from bheembhai.models.skill import Skill
from bheembhai.models.user import User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.providers.env_secrets import EnvSecureStorage
from sqlalchemy import delete, select

from conftest import FakeRuntime
from engine_service import worker as worker_mod
from engine_service.metrics import METRICS
from engine_service.recovery import recover_on_startup
from engine_service.run_init import InitFailure
from engine_service.runtime import RESULT_FILENAME
from engine_service.state_machine import drive_run
from engine_service.workflow import ExecState, Result, WorkflowSpec, resolve_model_tier

# One event loop for the whole module: the session-scoped async engine and its
# pool must never straddle pytest-asyncio's default function-scoped loops.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# ── Workflow/policy fixtures ────────────────────────────────────────────

WF_GATED = """
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

POLICY_GATE_FIRST = """
policy: strict
gates:
  story-design: {review: required, role: any}
"""

POLICY_GATE_MID = """
policy: strict-mid
gates:
  test-creator: {review: required, role: tech-lead}
"""

WF_BLOCK_ROUTE_TO = """
workflow: block-route
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: test-creator
      BLOCK: route_to
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

POLICY_GATE_BLOCK = """
policy: strict-block
gates:
  story-design: {review: required, role: any, on_status: [BLOCK]}
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


def _collector(events):
    """Async publish callback collecting engine events into `events`."""
    async def _append(event):
        events.append(event)
    return _append


@pytest.fixture
def config(app_config):
    return app_config


async def _skill(session, name):
    res = await session.execute(select(Skill).where(Skill.name == name))
    return res.scalar_one_or_none()


_PRESET_BRANCH = object()   # sentinel: default to a pre-set branch (init skips the network)


async def make_world(session, created, secure_storage, *,
                     wf_yaml=WF_GATED, pol_yaml=POLICY_GATE_FIRST,
                     state="pending", current_step=None,
                     step_overrides=None, run_branch=_PRESET_BRANCH):
    """Insert a complete run world (user/project/integrations/workflow/policy/
    skills/run/step rows) with credentials resolvable from SecureStorage.

    `run_branch` is preset by default so init never touches the network — the
    GitHub REST path is unit-tested with httpx mocks. Pass `run_branch=None` to
    force the derive-and-create path (monkeypatch `create_branch_github`).
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

    def integration(rtype, label, ref, cfg, secret):
        row = ProjectIntegration(project_id=project.id, type=rtype, label=label,
                                 credential_ref=ref, config=cfg)
        session.add(row)
        return row, secret

    gh, gh_secret = integration(
        "github", f"github-{suffix}", f"gh-ref-{suffix}",
        {"url": "https://github.com", "repository": "acme/demo", "base_branch": "main"},
        "ghp_abcd1234")
    vendor, vendor_secret = integration(
        "claude", f"claude-{suffix}", f"vendor-ref-{suffix}",
        {"model_high": "claude-A", "model_medium": "claude-B", "model_low": "claude-C"},
        "sk-ant-secret")
    jira, jira_secret = integration(
        "jira", f"jira-{suffix}", f"jira-ref-{suffix}",
        {"url": "https://team.atlassian.net", "username": f"t-{suffix}@test.co"},
        "jira-secret")
    await session.flush()
    created.extend((ProjectIntegration, r.id) for r in (gh, vendor, jira))

    await secure_storage.put(gh.credential_ref, gh_secret)
    await secure_storage.put(vendor.credential_ref, vendor_secret)
    await secure_storage.put(jira.credential_ref, jira_secret)

    # Skills are catalog rows — get-or-create (unique on name), never deleted.
    wf_spec = WorkflowSpec.load_yaml(wf_yaml)
    for sid, spec in wf_spec.steps.items():
        skill = spec.get("skill", sid)
        if await _skill(session, skill) is None:
            session.add(Skill(name=skill, description=f"test skill {skill}",
                              model="medium"))
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

    rb = f"feat/lnprtl-101/{suffix}" if run_branch is _PRESET_BRANCH else run_branch
    run = Run(project_id=project.id, workflow_id=workflow.id, policy_id=policy.id,
              story_id="LNPRTL-101", source_branch="main",
              run_branch=rb,
              github_integration_id=gh.id, jira_integration_id=jira.id,
              ai_vendor_integration_id=vendor.id, state=state,
              current_step=current_step, started_by_user_id=user.id)
    session.add(run)
    await session.flush()
    created.append((Run, run.id))

    for sid, spec in wf_spec.steps.items():
        row = Step(run_id=run.id, step_id=sid, skill=spec.get("skill", sid),
                   model_requested=resolve_model_tier(spec.get("model"), vendor.config))
        if step_overrides and sid in step_overrides:
            for k, v in step_overrides[sid].items():
                setattr(row, k, v)
        session.add(row)
    await session.commit()

    return {"run": run, "workflow": workflow, "policy": policy,
            "vendor_config": vendor.config}


def start_item(run, payload=None):
    return WorkQueueItem(run_id=run.id, action="start", payload=payload or {})


def continue_item(run, payload):
    return WorkQueueItem(run_id=run.id, action="continue", payload=payload)


async def get_run(session, run_id):
    await session.refresh(await session.get(Run, run_id))
    return await session.get(Run, run_id)


async def gate_transition(session, run_id):
    res = await session.execute(
        select(Transition)
        .where(Transition.run_id == run_id,
               Transition.to_state == ExecState.AWAITING_APPROVAL)
        .order_by(Transition.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def step_row(session, run_id, step_id):
    res = await session.execute(
        select(Step).where(Step.run_id == run_id, Step.step_id == step_id))
    return res.scalar_one_or_none()


# ── Core flows ──────────────────────────────────────────────────────────

async def test_start_drives_to_first_gate(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"], "implement": ["ok"]})
    events = []

    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    publish=_collector(events))

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"
    assert run.current_step == "story-design"
    row = await step_row(s, run.id, "story-design")
    assert row.exec_state == ExecState.COMPLETED
    assert row.result_status == Result.COMPLETED
    # gate card persisted on the awaiting_approval transition
    gate = await gate_transition(s, run.id)
    assert gate is not None
    assert gate.payload["role"] == "any"
    assert gate.payload["result_status"] == Result.COMPLETED
    assert gate.payload["summary"] == "story-design done"
    # only story-design ran; publish saw the approval event
    assert [c[0] for c in rt.calls] == ["story-design"]
    assert any(e["type"] == "approval_required" for e in events)
    # cost accumulated
    assert float(run.cost_usd) == 0.01


async def test_approve_drives_to_completion(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"], "implement": ["ok"]})
    events = []
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage,
                    publish=_collector(events))

    await drive_run(s, continue_item(world["run"], {
        "action": "approve", "actor": "reviewer@test.co", "comment": "lgtm"}),
        config, rt, secure_storage, publish=_collector(events))

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    assert [c[0] for c in rt.calls] == ["story-design", "test-creator", "implement"]
    # the decision is audited with the reviewer as actor
    res = await s.execute(
        select(Transition).where(
            Transition.run_id == run.id,
            Transition.from_state == ExecState.AWAITING_APPROVAL,
            Transition.to_state == ExecState.COMPLETED))
    decision = res.scalar_one()
    assert decision.actor == "reviewer@test.co"
    assert "lgtm" in decision.reason
    assert any(e["type"] == "run_completed" for e in events)
    # success verdicts carry no handoff
    tc_ctx = next(c for sid, c in rt.contexts if sid == "test-creator")
    assert tc_ctx["upstream_handoff"] is None


async def test_approve_honours_route_to_hint(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage,
                             wf_yaml=WF_BLOCK_ROUTE_TO, pol_yaml=POLICY_GATE_BLOCK)
    rt = FakeRuntime({"story-design": ["block"], "implement": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"          # BLOCK verdict hits the gate

    await drive_run(s, continue_item(world["run"], {"action": "approve"}),
                    config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    # the skill's next hint ("implement") was honoured under route_to
    assert [c[0] for c in rt.calls] == ["story-design", "implement"]


async def test_send_back_resets_and_replays(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage, pol_yaml=POLICY_GATE_MID)
    rt = FakeRuntime({"story-design": ["ok", "ok"], "test-creator": ["ok", "ok"],
                      "implement": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)
    run = await get_run(s, world["run"].id)
    assert run.state == "paused"
    assert run.current_step == "test-creator"

    await drive_run(s, continue_item(world["run"], {
        "action": "send_back", "send_back_to": "story-design",
        "actor": "pm@test.co", "comment": "tighten the acceptance criteria"}),
        config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"          # re-ran and hit the test-creator gate again
    assert run.current_step == "test-creator"
    # later steps were reset and the story-design re-run got the feedback
    tc = await step_row(s, run.id, "test-creator")
    imp = await step_row(s, run.id, "implement")
    assert tc.exec_state == ExecState.COMPLETED
    assert imp.exec_state == ExecState.PENDING
    assert imp.result_status is None
    sd_contexts = [c for sid, c in rt.contexts if sid == "story-design"]
    assert len(sd_contexts) == 2
    assert sd_contexts[1]["reviewer_feedback"] == "tighten the acceptance criteria"
    # decision audited with the sender as actor
    res = await s.execute(
        select(Transition).where(
            Transition.run_id == run.id,
            Transition.from_state == ExecState.AWAITING_APPROVAL,
            Transition.to_state == ExecState.COMPLETED,
            Transition.actor == "pm@test.co"))
    assert res.scalar_one() is not None


async def test_transient_failure_retries_in_fresh_container(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["crash", "ok"],
                      "test-creator": ["ok"], "implement": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    assert rt.calls == [("story-design", 1), ("story-design", 2),
                        ("test-creator", 1), ("implement", 1)]
    row = await step_row(s, run.id, "story-design")
    assert row.attempt_no == 2
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == ExecState.RETRYING))
    assert res.scalar_one() is not None


async def test_deterministic_failure_fails_run(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["exit-nonzero"],
                      "test-creator": ["ok"], "implement": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "failed"
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "failed",
                                 Transition.result_status == Result.FAILED_EXECUTION)
        .order_by(Transition.id.desc()).limit(1))
    assert res.scalar_one_or_none() is not None
    assert [c[0] for c in rt.calls] == ["story-design"]    # no retry, no next steps


async def test_block_verdict_hands_off_to_next_step(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage,
                             wf_yaml=WF_BLOCK_ROUTE_TO, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["block"], "implement": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "completed"
    imp_ctx = next(c for sid, c in rt.contexts if sid == "implement")
    handoff = imp_ctx["upstream_handoff"]
    assert handoff is not None
    assert handoff["from_step"] == "story-design"
    assert handoff["status"] == Result.BLOCK
    # the evidence channel: report files flow into the next step's context under
    # `report_files` — the key run_skill.sh reads (a reports/report_files mismatch
    # silently dropped the "Read its report first" clause from the prompt)
    assert handoff["report_files"] == ["docs/verification.md"]


async def test_visit_cap_halts_runaway_loop(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage,
                             wf_yaml=WF_SELF_LOOP, pol_yaml=POLICY_FAST)
    rt = FakeRuntime({"story-design": ["changes"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "failed"
    assert len(rt.calls) == 3    # cap (3) reached before the 4th visit launches
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "failed")
        .order_by(Transition.id.desc()).limit(1))
    assert "runaway loop" in res.scalar_one().reason


# ── Crash-resume ────────────────────────────────────────────────────────

async def test_crash_resume_relaunches_same_attempt(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage, state="running",
                             current_step="story-design",
                             step_overrides={"story-design": {
                                 "exec_state": ExecState.RUNNING, "attempt_no": 1,
                                 "fargate_task_arn": "container-abc",
                                 "started_at": datetime.now(timezone.utc)}})
    rt = FakeRuntime({"story-design": ["ok"]}, reattach_script={"story-design": "gone"})

    await drive_run(s, continue_item(world["run"], {"action": "resume"}),
                    config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"
    # the crashed container was gone → same attempt relaunched, not incremented
    assert rt.calls == [("story-design", 1)]
    row = await step_row(s, run.id, "story-design")
    assert row.attempt_no == 1
    assert row.result_status == Result.COMPLETED
    assert len(rt.rehandles) == 1      # re-attach was attempted first


async def test_crash_resume_reattaches_live_container(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage, state="running",
                             current_step="story-design",
                             step_overrides={"story-design": {
                                 "exec_state": ExecState.RUNNING, "attempt_no": 1,
                                 "fargate_task_arn": "container-abc",
                                 "started_at": datetime.now(timezone.utc)}})
    rt = FakeRuntime({"story-design": ["ok"]}, reattach_script={"story-design": "ok"})
    # simulate the pre-crash launch: the container ran and published its result
    # before the engine died. Seed the file the re-attached handle reconciles
    # against directly — launch() would record a call and break the assertion.
    outdir = rt._base / "results" / str(world["run"].id) / "story-design" / "1"
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / RESULT_FILENAME).write_text(json.dumps(
        {"status": "completed", "cost_usd": 0.01, "summary": "story-design done"}))

    await drive_run(s, continue_item(world["run"], {"action": "resume"}),
                    config, rt, secure_storage)

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"
    assert rt.calls == []            # the live container was adopted, not relaunched
    row = await step_row(s, run.id, "story-design")
    assert row.attempt_no == 1
    assert row.result_status == Result.COMPLETED


async def test_resume_at_open_gate_renotifies_without_rerunning(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"]})
    await drive_run(s, start_item(world["run"]), config, rt, secure_storage)
    run = await get_run(s, world["run"].id)
    assert run.state == "paused"

    events = []
    await drive_run(s, continue_item(world["run"], {"action": "resume"}),
                    config, rt, secure_storage, publish=_collector(events))

    run = await get_run(s, world["run"].id)
    assert run.state == "paused"        # still waiting for the human
    assert len(rt.calls) == 1           # nothing re-ran
    assert any(e["type"] == "approval_required" and e.get("renotified") for e in events)


# ── Worker dispatch guards ──────────────────────────────────────────────

async def test_init_failure_marks_run_failed_and_item_done(session, secure_storage, config,
                                                           monkeypatch):
    s, created = session
    world = await make_world(s, created, secure_storage, run_branch=None)

    async def fail_branch(*args, **kwargs):
        raise InitFailure("failed_execution", "GitHub returned HTTP 401: bad credentials")

    monkeypatch.setattr("engine_service.run_init.create_branch_github", fail_branch)
    rt = FakeRuntime({})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item = start_item(world["run"])
    s.add(item)
    await s.commit()
    await s.refresh(item)
    item.state = "claimed"
    item.claimed_by = worker_mod._claim_identity(config)
    item.claimed_at = datetime.now(timezone.utc)
    item.heartbeat_at = datetime.now(timezone.utc)
    await s.commit()

    await worker_mod._dispatch_guarded(config, item.id)

    run = await get_run(s, world["run"].id)
    assert run.state == "failed"
    res = await s.execute(
        select(Transition).where(Transition.run_id == run.id,
                                 Transition.to_state == "failed"))
    t = res.scalar_one()
    assert t.result_status == Result.FAILED_EXECUTION
    assert "401" in t.reason
    assert rt.calls == []            # no container was ever launched
    await s.refresh(item)
    assert item.state == "done"


async def test_run_source_branch_override_wins_over_integration_config(
        session, secure_storage, config, monkeypatch):
    """ADR-013 source-branch override: the run row carries the user's choice
    (or the submit-time fallback) — init must not let the live integration
    config (base_branch: main) clobber it."""
    s, created = session
    world = await make_world(s, created, secure_storage, run_branch=None)

    run_row = await get_run(s, world["run"].id)
    run_row.source_branch = "develop"          # what the platform persisted
    await s.commit()

    captured: dict[str, str] = {}

    async def fake_create_branch(git_target, token, source_branch, name):
        captured["source_branch"] = source_branch
        branch = f"feat/lnprtl-101/{uuid.uuid4().hex[:8]}"
        captured["run_branch"] = branch
        return branch

    monkeypatch.setattr("engine_service.run_init.create_branch_github",
                        fake_create_branch)
    rt = FakeRuntime({"story-design": ["ok"]})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item = start_item(world["run"])
    s.add(item)
    await s.commit()
    await s.refresh(item)
    item.state = "claimed"
    item.claimed_by = worker_mod._claim_identity(config)
    item.claimed_at = datetime.now(timezone.utc)
    item.heartbeat_at = datetime.now(timezone.utc)
    await s.commit()

    await worker_mod._dispatch_guarded(config, item.id)

    run = await get_run(s, world["run"].id)
    # The branch was cut off the run row's value — not the integration's "main".
    assert captured["source_branch"] == "develop"
    assert run.source_branch == "develop"
    assert run.run_branch == captured["run_branch"]
    assert run.state == "paused"     # first (gated) step completed after init


async def test_supersede_demotes_sibling_claims(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"]})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item_a = start_item(world["run"])
    item_b = continue_item(world["run"], {"action": "approve"})
    s.add_all([item_a, item_b])
    await s.commit()
    for it in (item_a, item_b):
        it.state = "claimed"
        it.claimed_by = worker_mod._claim_identity(config)
        it.claimed_at = datetime.now(timezone.utc)
        it.heartbeat_at = datetime.now(timezone.utc)
    await s.commit()

    await worker_mod._dispatch_guarded(config, item_a.id)

    await s.refresh(item_a)
    await s.refresh(item_b)
    assert item_a.state == "done"
    assert item_b.state == "pending"    # demoted, NOT done — decision still pending


async def test_lost_claim_aborts_dispatch(session, secure_storage, config):
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"]})
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage)

    item = start_item(world["run"])
    s.add(item)
    await s.commit()
    await s.refresh(item)
    item.state = "claimed"
    item.claimed_by = "engine-2"        # another engine owns it
    item.claimed_at = datetime.now(timezone.utc)
    item.heartbeat_at = datetime.now(timezone.utc)
    await s.commit()

    await worker_mod._dispatch_guarded(config, item.id)

    run = await get_run(s, world["run"].id)
    assert run.state == "pending"       # untouched
    assert rt.calls == []
    await s.refresh(item)
    assert item.state == "claimed"      # still owned by engine-2


# ── Recovery ────────────────────────────────────────────────────────────

async def test_recovery_reenqueues_stale_and_tops_up_resume_tokens(session,
                                                                   secure_storage,
                                                                   config):
    s, created = session
    # run A: stale claimed item — recovered to pending
    world_a = await make_world(s, created, secure_storage, state="running",
                               current_step="story-design")
    stale = start_item(world_a["run"])
    s.add(stale)
    await s.commit()
    await s.refresh(stale)
    stale.state = "claimed"
    stale.claimed_by = "dead-engine"
    stale.claimed_at = datetime.now(timezone.utc)
    stale.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    # run B: in-flight with NO item — gets a resume token
    world_b = await make_world(s, created, secure_storage, state="paused",
                               current_step="test-creator")
    # run C: in-flight with a pending item — untouched
    world_c = await make_world(s, created, secure_storage, state="running",
                               current_step="story-design")
    s.add(continue_item(world_c["run"], {"action": "resume"}))
    await s.commit()

    recovered = await recover_on_startup(config)

    assert recovered == 1
    await s.refresh(stale)
    assert stale.state == "pending"
    assert stale.claimed_by is None
    assert METRICS.orphaned_items == 1
    res = await s.execute(
        select(WorkQueueItem).where(WorkQueueItem.run_id == world_b["run"].id))
    tokens = res.scalars().all()
    assert len(tokens) == 1
    assert tokens[0].action == "continue"
    assert tokens[0].payload == {"action": "resume"}
    res = await s.execute(
        select(WorkQueueItem).where(WorkQueueItem.run_id == world_c["run"].id))
    assert len(res.scalars().all()) == 1    # its own token, nothing added


# ── Stale-claim reaping ───────────────────────────────────────────────────

def test_claim_identity_is_per_process(config):
    """claimed_by names a PROCESS: engine_id + a per-boot suffix — a restarted
    engine must not adopt (and keep fresh) the dead process's claims."""
    ident = worker_mod._claim_identity(config)
    assert ident.startswith(f"{config.engine.engine_id}:")
    assert len(ident) > len(config.engine.engine_id) + 1
    # memoized for the process lifetime
    assert worker_mod._claim_identity(config) == ident


async def test_reaper_demotes_stale_claim_leaves_fresh_and_pending(session,
                                                                   secure_storage,
                                                                   config):
    s, created = session
    # A: expired heartbeat — the claiming process is dead; reaped to pending.
    world_a = await make_world(s, created, secure_storage, state="running",
                               current_step="story-design")
    stale = start_item(world_a["run"])
    s.add(stale)
    await s.commit()
    await s.refresh(stale)
    stale.state = "claimed"
    stale.claimed_by = "dead-engine:abcd1234"
    stale.claimed_at = datetime.now(timezone.utc)
    stale.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    # B: fresh heartbeat — the claiming process is alive; untouched.
    world_b = await make_world(s, created, secure_storage, state="running",
                               current_step="story-design")
    fresh = start_item(world_b["run"])
    s.add(fresh)
    await s.commit()
    await s.refresh(fresh)
    fresh.state = "claimed"
    fresh.claimed_by = "live-engine:abcd1234"
    fresh.claimed_at = datetime.now(timezone.utc)
    fresh.heartbeat_at = datetime.now(timezone.utc)
    await s.commit()

    reaped = await worker_mod._reap_stale_claims(s, config)

    assert reaped == 1
    await s.refresh(stale)
    assert stale.state == "pending"
    assert stale.claimed_by is None
    assert stale.claimed_at is None
    assert stale.heartbeat_at is None
    await s.refresh(fresh)
    assert fresh.state == "claimed"     # still owned by the live process


async def test_worker_loop_reaps_then_reclaims_and_drives_run(session,
                                                              secure_storage,
                                                              config):
    """The deadlock this fixes end-to-end: a run whose dispatch died mid-step is
    left with a `claimed` item the restarted engine would previously keep fresh
    forever. Now the reaper demotes it on the next poll, the loop re-claims it,
    and the dispatch resumes the run from persisted state."""
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"], "implement": ["ok"]})
    events = []
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage,
                                publish=_collector(events))
    config.engine.poll_interval_seconds = 1  # speed the reap+claim loop

    item = start_item(world["run"])
    s.add(item)
    await s.commit()
    await s.refresh(item)
    item.state = "claimed"
    item.claimed_by = "dead-engine:abcd1234"
    item.claimed_at = datetime.now(timezone.utc)
    item.heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=1000)
    await s.commit()

    loop_task = asyncio.create_task(worker_mod.worker_loop(config))
    try:
        await _wait_until(
            lambda: _state_is(s, world["run"].id, "paused")
            and _items_done(s, world["run"].id),
            "reaped claim re-queued and the run reaching its gate")
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass

    assert rt.calls == [("story-design", 1)]
    await s.refresh(item)
    assert item.state == "done"


# ── M5 review gate: e2e mock run with a real gate decision ────────────────

async def _state_is(s, run_id, state) -> bool:
    res = await s.execute(select(Run.state).where(Run.id == run_id))
    return res.scalar_one() == state


async def _items_done(s, run_id) -> bool:
    res = await s.execute(
        select(WorkQueueItem.state).where(WorkQueueItem.run_id == run_id))
    states = res.scalars().all()
    return bool(states) and all(st == "done" for st in states)


async def _wait_until(predicate, what, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"timed out waiting for {what}")


async def test_e2e_mock_run_with_real_gate_decision_via_worker_loop(
        session, secure_storage, config):
    """M5 review gate — the platform's queued decision drives the run through the
    REAL worker loop: claim (SKIP LOCKED) → dispatch → drive_run → item done.

    This is the full production path with exactly two seams faked: the Runtime
    (FakeRuntime — no Docker) and the engine→platform push (collected events —
    no HTTP). The continue token carries the exact payload shape the platform's
    POST /api/runs/{id}/decision now writes.
    """
    s, created = session
    world = await make_world(s, created, secure_storage)
    rt = FakeRuntime({"story-design": ["ok"], "test-creator": ["ok"], "implement": ["ok"]})
    events = []
    worker_mod.configure_worker(runtime=rt, secure_storage=secure_storage,
                                publish=_collector(events))
    config.engine.poll_interval_seconds = 1  # speed the claim loop

    # Platform run-submit: enqueue a start token (POST /api/runs writes this row).
    s.add(start_item(world["run"]))
    await s.commit()

    loop_task = asyncio.create_task(worker_mod.worker_loop(config))
    try:
        await _wait_until(
            lambda: _state_is(s, world["run"].id, "paused"),
            "run reaching the story-design gate")

        # Platform gate decision — POST /api/runs/{id}/decision enqueues exactly
        # this continue token, leaving run.state untouched.
        s.add(continue_item(world["run"], {
            "action": "approve", "actor": "reviewer@test.co", "comment": "lgtm"}))
        await s.commit()

        await _wait_until(
            lambda: _state_is(s, world["run"].id, "completed")
            and _items_done(s, world["run"].id),
            "run completing after the queued approve decision")

        assert [c[0] for c in rt.calls] == ["story-design", "test-creator", "implement"]
        # the decision is audited with the platform-supplied actor
        decision = (await s.execute(select(Transition).where(
            Transition.run_id == world["run"].id,
            Transition.from_state == ExecState.AWAITING_APPROVAL,
            Transition.to_state == ExecState.COMPLETED))).scalar_one()
        assert decision.actor == "reviewer@test.co"
        assert "lgtm" in decision.reason
        assert any(e["type"] == "approval_required" for e in events)
        assert any(e["type"] == "run_completed" for e in events)
    finally:
        loop_task.cancel()
        try:
            await loop_task
        except asyncio.CancelledError:
            pass
