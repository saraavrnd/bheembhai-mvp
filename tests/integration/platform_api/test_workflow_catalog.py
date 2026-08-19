"""Integration — the Workflows tab catalog and the workflow home endpoints.

Covers the user-facing contract of the new tab:

- GET /api/projects/{pid}/workflow-catalog: category rail (only categories
  with active project workflows), workflow cards (grouped counts, in-flight,
  last-run), recent runs (≤15 newest first, needs_review for paused).
- GET /api/workflows/{id}/home: definition, newest ACTIVE policy gates,
  30-day stats with hand-computed counters, awaiting-review from paused runs
  (payload gate role wins over the active policy's role), scoped run list
  with executions/loop-backs. Platform templates 404; non-members 403.

Runs and transitions are inserted directly via the test's own session with
explicit created_at/ts so the stat math is deterministic.

Loop scoping: same pattern as test_workflow_categories.py — a dedicated
engine on the test's own loop, the platform app on TestClient's portal loop.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from bheembhai.models.project import Project
from bheembhai.models.run import Run, Transition
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
DEV_EMAIL = "dev@bheembhai.local"

# Transition timestamps land 5 days back — comfortably inside the 30-day
# stats window while keeping relative-time strings non-zero.
T0 = time.time() - 5 * 86400

WF_YAML = """workflow: {name}
version: 1
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    label: Design the story
    deadline: 900
    "on":
      completed: implement
  - id: implement
    skill: implement
    model: medium
    label: Implement
    deadline: 1800
    "on":
      completed: code-review
      changes_requested: implement
  - id: code-review
    skill: code-review
    model: high
    label: Review code
    deadline: 900
    "on":
      completed: DONE
      changes_requested: implement
"""

POLICY_YAML = """policy: strict
version: 1
applies_to: story-delivery
gates:
  story-design: {{review: required, role: any}}
  code-review: {{review: required, role: {code_review_role}}}
"""


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
    """Dev user (promoted to ADMIN) + a project they manage (PM membership)."""
    suffix = uuid.uuid4().hex[:8]
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one_or_none()
        if user is None:
            user = User(external_id=DEV_USER[0], auth_provider=DEV_USER[1],
                        email=DEV_EMAIL, display_name="Dev User")
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
    """Delete the project (cascades project workflows/policies/runs/membership),
    platform workflows, and categories created by the test; restore the role."""
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


# ── Direct-insert helpers ────────────────────────────────────────────────────


def _tr(step_id, from_state, to_state, ts, *, result_status=None, reason=None,
        actor="system", payload=None) -> dict:
    return {"step_id": step_id, "from_state": from_state, "to_state": to_state,
            "ts": ts, "result_status": result_status, "reason": reason,
            "actor": actor, "payload": payload}


async def _add_workflow(project_id, name, category_id, *, description="",
                        is_active=True, yaml=None) -> uuid.UUID:
    async with _sm() as session:
        wf = Workflow(
            project_id=project_id, name=name, description=description,
            yaml_content=yaml or WF_YAML.format(name=name),
            is_active=is_active, workflow_category_id=category_id,
        )
        session.add(wf)
        await session.commit()
        return wf.id


async def _add_policy(project_id, workflow_id, name, *, code_review_role="lead",
                      is_active=True, created_days_ago=0) -> uuid.UUID:
    async with _sm() as session:
        pol = Policy(
            project_id=project_id, workflow_id=workflow_id, name=name,
            yaml_content=POLICY_YAML.format(code_review_role=code_review_role),
            is_active=is_active,
            created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        )
        session.add(pol)
        await session.commit()
        return pol.id


async def _add_run(project_id, workflow_id, policy_id, *, story_id, state,
                   current_step=None, created_days_ago=0,
                   transitions: list[dict]) -> uuid.UUID:
    async with _sm() as session:
        run = Run(
            project_id=project_id, workflow_id=workflow_id, policy_id=policy_id,
            story_id=story_id, source_branch="main", state=state,
            current_step=current_step,
            created_at=datetime.now(timezone.utc) - timedelta(days=created_days_ago),
        )
        session.add(run)
        await session.flush()
        for t in transitions:
            session.add(Transition(
                run_id=run.id, step_id=t["step_id"], attempt_no=1,
                from_state=t["from_state"], to_state=t["to_state"], ts=t["ts"],
                result_status=t["result_status"], reason=t["reason"],
                actor=t["actor"], payload=t["payload"],
            ))
        await session.commit()
        return run.id


# ── Catalog ──────────────────────────────────────────────────────────────────


async def test_catalog_counts_grouped_by_category_and_workflow(client):
    world = await _make_world()
    suffix = world["suffix"]
    pid = world["project"]
    cat_ids, wf_ids = [], []
    try:
        cat1 = client.post("/api/admin/workflow-categories",
                           json={"name": f"Alpha-{suffix}"}).json()
        cat2 = client.post("/api/admin/workflow-categories",
                           json={"name": f"Beta-{suffix}"}).json()
        cat_ids += [cat1["id"], cat2["id"]]

        wf1 = await _add_workflow(pid, f"delivery-{suffix}", cat1["id"],
                                  description="Seven-step delivery")
        wf2 = await _add_workflow(pid, f"hotfix-{suffix}", cat1["id"])
        wf3 = await _add_workflow(pid, f"refactor-{suffix}", cat2["id"])
        wf_inactive = await _add_workflow(pid, f"retired-{suffix}", cat1["id"],
                                          is_active=False)
        wf_platform = await _add_workflow(None, f"template-{suffix}", cat1["id"])
        wf_ids += [wf_inactive, wf_platform]

        for wf in (wf1, wf2, wf3):
            await _add_policy(pid, wf, f"pol-{wf}", code_review_role="lead")

        # Runs: wf1 has a completed + a running run, wf2 a paused gate,
        # wf3 a failed run — created 1..4 days ago (newest first). The
        # run-specific policies are INACTIVE so they don't feed gate_count
        # (which only reads active policies).
        r1 = await _add_run(pid, wf1, (await _add_policy(pid, wf1, "r1-policy",
                                                         is_active=False)),
                            story_id=f"STORY-{suffix}-1", state="completed",
                            created_days_ago=1, transitions=[
            _tr("", "pending", "running", T0, reason="branch created: feat/x"),
            _tr("story-design", "pending", "running", T0 + 10),
            _tr("story-design", "awaiting_result", "completed", T0 + 100,
                result_status="completed", payload={"commit": "abc1234"}),
            _tr("", "running", "completed", T0 + 100),
        ])
        r2 = await _add_run(pid, wf1, (await _add_policy(pid, wf1, "r2-policy",
                                                         is_active=False)),
                            story_id=f"STORY-{suffix}-2", state="running",
                            created_days_ago=2, transitions=[
            _tr("", "pending", "running", T0, reason="branch created: feat/x"),
            _tr("implement", "pending", "running", T0 + 10),
        ])
        r3 = await _add_run(pid, wf2, (await _add_policy(pid, wf2, "r3-policy",
                                                         is_active=False)),
                            story_id=f"STORY-{suffix}-3", state="paused",
                            current_step="code-review", created_days_ago=3,
                            transitions=[
            _tr("code-review", "completed", "awaiting_approval", T0 + 10,
                payload={"role": "lead", "result_status": "completed"}),
        ])
        r4 = await _add_run(pid, wf3, (await _add_policy(pid, wf3, "r4-policy",
                                                         is_active=False)),
                            story_id=f"STORY-{suffix}-4", state="failed",
                            created_days_ago=4, transitions=[
            _tr("", "pending", "running", T0),
            _tr("", "running", "failed", T0 + 50),
        ])

        resp = client.get(f"/api/projects/{pid}/workflow-catalog")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        # Categories: only Alpha and Beta carry active project workflows.
        assert [c["name"] for c in data["categories"]] == [
            f"Alpha-{suffix}", f"Beta-{suffix}"]
        alpha = next(c for c in data["categories"] if c["name"] == f"Alpha-{suffix}")
        beta = next(c for c in data["categories"] if c["name"] == f"Beta-{suffix}")
        assert alpha["workflow_count"] == 2
        # Paused runs count as in-flight (they sit at a human gate).
        assert alpha["in_flight"] == 2
        assert beta["workflow_count"] == 1
        assert beta["in_flight"] == 0

        # Cards: active project workflows only — inactive + platform excluded.
        cards = data["workflows"]
        card_ids = {c["id"] for c in cards}
        assert card_ids == {str(wf1), str(wf2), str(wf3)}
        assert str(wf_inactive) not in card_ids
        assert str(wf_platform) not in card_ids

        wf1_card = next(c for c in cards if c["id"] == str(wf1))
        assert wf1_card["name"] == f"delivery-{suffix}"
        assert wf1_card["description"] == "Seven-step delivery"
        assert wf1_card["category_id"] == cat1["id"]
        assert wf1_card["category_name"] == f"Alpha-{suffix}"
        assert wf1_card["steps"] == 3
        assert wf1_card["policy_count"] == 3   # 2 run policies + 1 active
        assert wf1_card["gate_count"] == 2
        assert wf1_card["in_flight"] == 1
        last = wf1_card["last_run"]
        assert last["run_id"] == str(r1)
        assert last["story_id"] == f"STORY-{suffix}-1"
        assert last["state"] == "completed"
        assert isinstance(last["relative"], str) and last["relative"].endswith("ago")

        wf2_card = next(c for c in cards if c["id"] == str(wf2))
        assert wf2_card["in_flight"] == 1   # the paused gate counts
        assert wf2_card["last_run"]["state"] == "paused"

        wf3_card = next(c for c in cards if c["id"] == str(wf3))
        assert wf3_card["in_flight"] == 0
        assert wf3_card["last_run"]["state"] == "failed"
        assert wf3_card["last_run"]["run_id"] == str(r4)

        # Recent runs: newest first, paused flagged for review.
        recent = data["recent_runs"]
        assert [r["run_id"] for r in recent] == [
            str(r1), str(r2), str(r3), str(r4)]
        assert recent[0]["workflow_name"] == f"delivery-{suffix}"
        assert recent[0]["needs_review"] is False
        assert recent[2]["needs_review"] is True
        assert isinstance(recent[0]["updated"], str)
        assert isinstance(recent[0]["elapsed"], str)
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


async def test_catalog_requires_membership(client):
    world = await _make_world()
    pid = world["project"]
    try:
        # Demote the dev user — ADMINs bypass the membership check.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "USER"
            await session.commit()
        async with _sm() as session:
            await session.execute(delete(Membership).where(
                Membership.user_id == world["user"],
                Membership.project_id == pid))
            await session.commit()
        resp = client.get(f"/api/projects/{pid}/workflow-catalog")
        assert resp.status_code == 403, resp.text

        # Restore ADMIN so _cleanup's role restore is accurate.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "ADMIN"
            await session.commit()
    finally:
        await _cleanup(world)


# ── Workflow home ────────────────────────────────────────────────────────────


async def _make_home_world(client) -> dict:
    """One project workflow with three policies and five runs whose stat
    math is hand-computed (see the assertions in the test below)."""
    world = await _make_world()
    suffix = world["suffix"]
    pid = world["project"]
    cat = client.post("/api/admin/workflow-categories",
                      json={"name": f"Home-{suffix}"}).json()
    wf = await _add_workflow(pid, f"home-wf-{suffix}", cat["id"],
                             description="The delivery pipeline")
    # Newest ACTIVE policy wins the definition strip: pol_owner (1d ago).
    pol_old = await _add_policy(pid, wf, "governed-v1",
                                code_review_role="lead", created_days_ago=10)
    pol_ba = await _add_policy(pid, wf, "ba-policy-v1",
                               code_review_role="ba", is_active=False,
                               created_days_ago=2)
    pol_owner = await _add_policy(pid, wf, "strict-v1",
                                  code_review_role="product_owner",
                                  created_days_ago=1)

    # A: completed — gated design (300s wait), then a changes_requested
    # loop-back implement→code-review→implement. executions=4, loop_backs=1.
    run_a = await _add_run(pid, wf, pol_owner, story_id=f"STORY-{suffix}-A",
                           state="completed", created_days_ago=1, transitions=[
        _tr("", "pending", "running", T0, reason="branch created: feat/x"),
        _tr("story-design", "pending", "running", T0 + 10),
        _tr("story-design", "awaiting_result", "completed", T0 + 100,
            result_status="completed", payload={"commit": "abc1234"}),
        _tr("story-design", "completed", "awaiting_approval", T0 + 100,
            result_status="completed",
            payload={"result_status": "completed", "role": "lead"}),
        _tr("story-design", "awaiting_approval", "completed", T0 + 400,
            result_status="completed", actor=DEV_EMAIL,
            reason="reviewer chose: approve — LGTM"),
        _tr("implement", "pending", "running", T0 + 400),
        _tr("implement", "awaiting_result", "completed", T0 + 500,
            result_status="completed"),
        _tr("code-review", "pending", "running", T0 + 500),
        _tr("code-review", "awaiting_result", "failed", T0 + 600,
            result_status="changes_requested"),
        _tr("implement", "pending", "running", T0 + 600),
        _tr("implement", "awaiting_result", "completed", T0 + 700,
            result_status="completed"),
        _tr("", "running", "completed", T0 + 800, reason="run completed"),
    ])

    # B: failed. executions=1, duration=200.
    run_b = await _add_run(pid, wf, pol_owner, story_id=f"STORY-{suffix}-B",
                           state="failed", created_days_ago=2, transitions=[
        _tr("", "pending", "running", T0),
        _tr("implement", "pending", "running", T0 + 50),
        _tr("implement", "awaiting_result", "failed", T0 + 200,
            result_status="failed_execution"),
        _tr("", "running", "failed", T0 + 200),
    ])

    # C: cancelled while gated — the cancel-close is NOT a human wait.
    # executions=1, duration=150, no gate wait.
    run_c = await _add_run(pid, wf, pol_owner, story_id=f"STORY-{suffix}-C",
                           state="cancelled", created_days_ago=3, transitions=[
        _tr("", "pending", "running", T0),
        _tr("story-design", "pending", "running", T0 + 20),
        _tr("story-design", "awaiting_result", "completed", T0 + 100,
            result_status="completed", payload={"commit": "cafe1234"}),
        _tr("story-design", "completed", "awaiting_approval", T0 + 101,
            payload={"result_status": "completed", "role": "lead"}),
        _tr("story-design", "awaiting_approval", "completed", T0 + 150,
            actor=DEV_EMAIL,
            reason="gate closed — run cancelled by " + DEV_EMAIL),
        _tr("", "running", "cancelled", T0 + 150),
    ])

    # D: paused at code-review under its OWN (inactive) policy with role "ba"
    # — the payload role must win over the active policy's "product_owner".
    run_d = await _add_run(pid, wf, pol_ba, story_id=f"STORY-{suffix}-D",
                           state="paused", current_step="code-review",
                           created_days_ago=4, transitions=[
        _tr("story-design", "pending", "running", T0 + 10),
        _tr("story-design", "awaiting_result", "completed", T0 + 60,
            result_status="completed"),
        _tr("code-review", "pending", "running", T0 + 60),
        _tr("code-review", "awaiting_result", "completed", T0 + 120,
            result_status="completed"),
        _tr("code-review", "completed", "awaiting_approval", T0 + 121,
            payload={"result_status": "completed", "role": "ba"}),
    ])

    # E: paused but 40 days old — outside the stats window, still awaiting
    # review. Its gate payload carries no role → falls back to the active
    # policy's gate role for story-design ("any").
    run_e = await _add_run(pid, wf, pol_ba, story_id=f"STORY-{suffix}-E",
                           state="paused", current_step="story-design",
                           created_days_ago=40, transitions=[
        _tr("story-design", "pending", "running", T0),
        _tr("story-design", "awaiting_result", "completed", T0 + 50,
            result_status="completed"),
        _tr("story-design", "completed", "awaiting_approval", T0 + 51,
            payload={"result_status": "completed"}),
    ])

    world["suffix"] = suffix
    world["category"] = cat
    world["wf"] = wf
    world["runs"] = {"A": run_a, "B": run_b, "C": run_c, "D": run_d, "E": run_e}
    world["policies"] = {"old": pol_old, "ba": pol_ba, "owner": pol_owner}
    return world


async def test_home_stats_and_awaiting_review(client):
    world = await _make_home_world(client)
    runs = world["runs"]
    try:
        resp = client.get(f"/api/workflows/{world['wf']}/home")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        wf = data["workflow"]
        assert wf["name"] == f"home-wf-{world['suffix']}"
        assert wf["description"] == "The delivery pipeline"
        assert wf["category_id"] == world["category"]["id"]
        assert wf["category_name"] == f"Home-{world['suffix']}"
        assert [s["id"] for s in wf["parsed"]["steps"]] == [
            "story-design", "implement", "code-review"]

        # Newest ACTIVE policy wins (pol_ba is inactive, pol_old older).
        pol = data["active_policy"]
        assert pol["name"] == "strict-v1"
        assert pol["gates"]["code-review"]["role"] == "product_owner"
        assert pol["gates"]["story-design"]["role"] == "any"

        stats = data["stats"]
        # E (40d) is outside the 30-day window.
        assert stats["runs_total"] == 4
        assert stats["by_state"] == {"completed": 1, "failed": 1, "cancelled": 1}
        assert stats["live"] == 1
        # Durations: A=800, B=200, C=150 → median 200.
        assert stats["median_duration_s"] == 200.0
        # Gate waits: only A's human decision counts (C's cancel-close doesn't).
        assert stats["median_gate_wait_s"] == 300.0
        assert stats["loop_back_rate_pct"] == 25.0
        assert stats["most_common_loop_edge"] == {
            "from_step": "code-review", "to_step": "implement", "count": 1}

        awaiting = stats["awaiting_review"]
        assert awaiting["total"] == 2
        by_run = {i["run_id"]: i for i in awaiting["items"]}
        # Payload role (run D's own policy) beats the active policy's role.
        assert by_run[str(runs["D"])]["gate_step"] == "code-review"
        assert by_run[str(runs["D"])]["gate_role"] == "ba"
        # No role in the payload → active policy's gate role.
        assert by_run[str(runs["E"])]["gate_step"] == "story-design"
        assert by_run[str(runs["E"])]["gate_role"] == "any"

        # Scoped run list: newest first, E excluded, counters per run.
        home_runs = data["runs"]
        assert [r["run_id"] for r in home_runs] == [
            str(runs["A"]), str(runs["B"]), str(runs["C"]), str(runs["D"])]
        a = home_runs[0]
        assert a["story_id"] == f"STORY-{world['suffix']}-A"
        assert a["state"] == "completed"
        assert a["needs_review"] is False
        assert a["executions"] == 4
        assert a["loop_backs"] == 1
        assert isinstance(a["updated"], str)
        b, c, d = home_runs[1], home_runs[2], home_runs[3]
        assert (b["executions"], b["loop_backs"]) == (1, 0)
        assert (c["executions"], c["loop_backs"]) == (1, 0)
        assert d["needs_review"] is True
        assert (d["executions"], d["loop_backs"]) == (2, 0)
    finally:
        await _cleanup(world, category_ids=[world["category"]["id"]])


async def test_home_404_for_unknown_and_platform_templates(client):
    world = await _make_world()
    suffix = world["suffix"]
    cat_ids, wf_ids = [], []
    try:
        cat = client.post("/api/admin/workflow-categories",
                          json={"name": f"Plat-{suffix}"}).json()
        cat_ids.append(cat["id"])
        platform_wf = await _add_workflow(None, f"template-{suffix}", cat["id"])
        wf_ids.append(platform_wf)

        # Unknown workflow → 404
        resp = client.get(f"/api/workflows/{uuid.uuid4()}/home")
        assert resp.status_code == 404, resp.text

        # Platform templates have no run history → 404 (also hides the id)
        resp = client.get(f"/api/workflows/{platform_wf}/home")
        assert resp.status_code == 404, resp.text
    finally:
        await _cleanup(world, workflow_ids=wf_ids, category_ids=cat_ids)


async def test_home_requires_membership(client):
    world = await _make_home_world(client)
    pid = world["project"]
    try:
        # Demote the dev user — ADMINs bypass the membership check.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "USER"
            await session.commit()
        async with _sm() as session:
            await session.execute(delete(Membership).where(
                Membership.user_id == world["user"],
                Membership.project_id == pid))
            await session.commit()
        resp = client.get(f"/api/workflows/{world['wf']}/home")
        assert resp.status_code == 403, resp.text

        # Restore ADMIN so _cleanup's role restore is accurate.
        async with _sm() as session:
            user = await session.get(User, world["user"])
            user.platform_role = "ADMIN"
            await session.commit()
    finally:
        await _cleanup(world, category_ids=[world["category"]["id"]])
