"""Integration — admin workflow zip import: two-phase flow over real HTTP +
Postgres.

Analyze (stateless) returns the analysis table with scope-aware exists flags
(platform vs project, plus a ``platform_exists`` hint on skills); import
re-uploads the same zip plus namespaced per-row decisions and writes
Workflow/Policy/Skill rows. Imported workflows and policies land
``is_active=False``; categories match by name and are created when missing;
project scope creates shadowing project skills instead of touching platform
rows.

Admin gate: the dev user is promoted to platform ADMIN per test and restored
in the world fixture teardown (same idiom as test_skill_import_api.py).
"""

import io
import json
import uuid
import zipfile

import pytest
import pytest_asyncio
import yaml
from bheembhai.models.project import Project
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.workflow_category import WorkflowCategory
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

TEST_DB_URL = "postgresql+asyncpg://bheembhai-mvp:bheembhai-mvp@localhost:5555/bheembhai_test"

# Dedicated engine for the test's OWN loop (see test_project_skills.py docstring).
_engine = create_async_engine(TEST_DB_URL)
_sm = async_sessionmaker(_engine, expire_on_commit=False)

# The DEV_AUTH_BYPASS identity (dependencies.py) — the world fixture promotes
# it to ADMIN because require_admin checks the DB row's platform_role.
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


@pytest_asyncio.fixture(loop_scope="session")
async def world():
    """ADMIN dev user + a scratch project + a unique name suffix.

    Teardown restores the dev user's role FIRST (a failed delete must never
    leave the shared dev user stuck as ADMIN), then removes every row created
    under the suffix — project scope by project_id, platform scope by name —
    and finally the scratch project.
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
        project = Project(name=f"imp-{suffix}-project", description="",
                          owner_id=user.id)
        session.add(project)
        await session.commit()
        project_id = project.id

    yield {"suffix": suffix, "prev_role": prev_role, "project_id": project_id}

    async with _sm() as session:
        # Role restore FIRST (see docstring).
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one()
        user.platform_role = prev_role
        # Project scope: policies before workflows (workflow_id FK is NO ACTION).
        await session.execute(delete(Policy).where(Policy.project_id == project_id))
        await session.execute(delete(Workflow).where(Workflow.project_id == project_id))
        await session.execute(delete(Skill).where(Skill.project_id == project_id))
        # Platform scope rows created under the suffix.
        await session.execute(delete(Skill).where(
            Skill.project_id.is_(None), Skill.name.like(f"%{suffix}%")))
        await session.execute(delete(Policy).where(
            Policy.project_id.is_(None), Policy.name.like(f"%{suffix}%")))
        await session.execute(delete(Workflow).where(
            Workflow.project_id.is_(None), Workflow.name.like(f"%{suffix}%")))
        # Categories are shared reference data — remove only match-or-created ones.
        await session.execute(delete(WorkflowCategory).where(
            WorkflowCategory.name.like(f"%{suffix}%")))
        await session.execute(delete(Project).where(Project.id == project_id))
        await session.commit()


# ── helpers ─────────────────────────────────────────────────────────────────


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _wf_yaml(skills=()) -> str:
    steps = "\n".join(
        f"  - id: s{i}\n    skill: {s}\n" for i, s in enumerate(skills)
    )
    return f"workflow: wf\nversion: 1\nstart: s0\nsteps:\n{steps}"


def _wf_doc(name, version=1, content=None, category=None, is_active=True) -> bytes:
    doc = {"name": name, "version": version, "description": "",
           "is_active": is_active}
    if category:
        doc["category"] = category
    doc["content"] = content if content is not None else _wf_yaml()
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _pol_doc(name, version=1, content=None) -> bytes:
    doc = {"name": name, "version": version, "description": "",
           "is_active": True}
    doc["content"] = content or f"policy: {name}\nversion: 1\ngates: {{}}\n"
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _md(frontmatter: str, body: str = "") -> bytes:
    return f"---\n{frontmatter}\n---\n{body}\n".encode()


def _analyze(client, zip_bytes, project_id=None):
    data = {}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post(
        "/api/admin/workflows/import/analyze",
        files={"zip_file": ("workflows.zip", zip_bytes, "application/zip")},
        data=data,
    )


def _do_import(client, zip_bytes, decisions, project_id=None):
    data = {"decisions": json.dumps(decisions)}
    if project_id is not None:
        data["project_id"] = str(project_id)
    return client.post(
        "/api/admin/workflows/import",
        files={"zip_file": ("workflows.zip", zip_bytes, "application/zip")},
        data=data,
    )


async def _platform_workflow(name) -> Workflow | None:
    async with _sm() as session:
        return (await session.execute(select(Workflow).where(
            Workflow.name == name,
            Workflow.project_id.is_(None)))).scalar_one_or_none()


async def _skill_files(skill_id) -> dict[str, str]:
    async with _sm() as session:
        rows = (await session.execute(select(SkillFile).where(
            SkillFile.skill_id == skill_id))).scalars().all()
        return {row.path: row.content for row in rows}


# ── analyze phase ────────────────────────────────────────────────────────────


async def test_import_endpoints_are_admin_gated(client, world):
    """Non-ADMIN → 403 (world promotes to ADMIN; demote here explicitly)."""
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one()
        user.platform_role = "USER"
        await session.commit()

    resp = _analyze(client, _zip({
        "workflows/w.yaml": _wf_doc("w"),
    }))
    assert resp.status_code == 403


async def test_analyze_scope_aware_exists_and_platform_exists(client, world):
    """exists flags depend on the scope: a project workflow does not count in
    platform scope and vice versa; project scope additionally reports
    ``platform_exists`` on skills that share a platform skill's name."""
    s = world["suffix"]
    pid = world["project_id"]
    plat_wf = f"imp-{s}-plat-wf"
    proj_wf = f"imp-{s}-proj-wf"
    plat_skill = f"imp-{s}-plat-skill"
    proj_skill = f"imp-{s}-proj-skill"
    plat_only_skill = f"imp-{s}-u-only"
    plat_pol = f"imp-{s}-pol"
    proj_pol = f"imp-{s}-proj-pol"

    async with _sm() as session:
        session.add_all([
            Skill(name=plat_skill, description="d", model="medium"),
            Skill(name=plat_only_skill, description="d", model="medium"),
            Skill(name=proj_skill, description="d", model="medium",
                  project_id=pid),
        ])
        await session.flush()
        session.add_all([
            SkillFile(skill_id=(await session.execute(
                select(Skill.id).where(Skill.name == plat_skill))).scalar_one(),
                      path="SKILL.md", content="# p"),
            SkillFile(skill_id=(await session.execute(
                select(Skill.id).where(Skill.name == plat_only_skill))).scalar_one(),
                      path="SKILL.md", content="# u"),
            SkillFile(skill_id=(await session.execute(
                select(Skill.id).where(Skill.name == proj_skill))).scalar_one(),
                      path="SKILL.md", content="# j"),
        ])
        wf_plat = Workflow(name=plat_wf, version=1, description="",
                           yaml_content=_wf_yaml((plat_skill,)), is_active=True)
        wf_proj = Workflow(name=proj_wf, version=1, description="",
                           yaml_content=_wf_yaml((proj_skill,)), is_active=True,
                           project_id=pid)
        session.add_all([wf_plat, wf_proj])
        await session.flush()
        session.add_all([
            Policy(workflow_id=wf_plat.id, name=plat_pol, version=1,
                   yaml_content=f"policy: {plat_pol}\n"),
            Policy(workflow_id=wf_proj.id, name=proj_pol, version=1,
                   yaml_content=f"policy: {proj_pol}\n", project_id=pid),
        ])
        await session.commit()

    fresh_wf = f"imp-{s}-fresh-wf"
    zip_bytes = _zip({
        "workflows/x.yaml": _wf_doc(plat_wf),
        "workflows/y.yaml": _wf_doc(proj_wf),
        "workflows/z.yaml": _wf_doc(fresh_wf),
        "policies/x/p.yaml": _pol_doc(plat_pol),
        "policies/y/py.yaml": _pol_doc(proj_pol),
        f"skills/{plat_skill}/SKILL.md": _md(f"name: {plat_skill}"),
        f"skills/{proj_skill}/SKILL.md": _md(f"name: {proj_skill}"),
        f"skills/{plat_only_skill}/SKILL.md": _md(f"name: {plat_only_skill}"),
    })

    # Platform scope: only platform rows exist.
    body = _analyze(client, zip_bytes).json()
    wfs = {row["name"]: row for row in body["workflows"]}
    assert wfs[plat_wf]["exists"] is True
    assert wfs[proj_wf]["exists"] is False
    assert wfs[fresh_wf]["exists"] is False
    skills = {row["name"]: row for row in body["skills"]}
    assert skills[plat_skill]["exists"] is True
    assert skills[proj_skill]["exists"] is False
    assert skills[proj_skill]["platform_exists"] is False
    assert skills[plat_only_skill]["exists"] is True
    pols = {(row["workflow"], row["name"]): row for row in body["policies"]}
    assert pols[(plat_wf, plat_pol)]["exists"] is True
    assert pols[(proj_wf, proj_pol)]["exists"] is False

    # Project scope: project rows exist; platform skills become
    # platform_exists=True instead of exists.
    body = _analyze(client, zip_bytes, project_id=pid).json()
    wfs = {row["name"]: row for row in body["workflows"]}
    assert wfs[plat_wf]["exists"] is False
    assert wfs[proj_wf]["exists"] is True
    skills = {row["name"]: row for row in body["skills"]}
    assert skills[plat_skill]["exists"] is False
    assert skills[plat_skill]["platform_exists"] is True
    assert skills[proj_skill]["exists"] is True
    assert skills[plat_only_skill]["platform_exists"] is True
    pols = {(row["workflow"], row["name"]): row for row in body["policies"]}
    assert pols[(plat_wf, plat_pol)]["exists"] is False
    assert pols[(proj_wf, proj_pol)]["exists"] is True


async def test_analyze_rejects_zip_without_workflows_folder(client, world):
    resp = _analyze(client, _zip({"skills/x/SKILL.md": _md("name: x")}))
    assert resp.status_code == 422
    assert "no workflows/ folder" in resp.json()["detail"]


async def test_analyze_unknown_project_404(client, world):
    zip_bytes = _zip({"workflows/w.yaml": _wf_doc("w")})
    resp = _analyze(client, zip_bytes, project_id=uuid.uuid4())
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


# ── import phase ─────────────────────────────────────────────────────────────


async def test_import_creates_rows_inactive_with_category(client, world):
    """Fresh zip → workflow + policy + skill rows; imported workflows and
    policies land inactive; a missing category is created by name."""
    s = world["suffix"]
    wf_name = f"imp-{s}-new-wf"
    pol_name = f"imp-{s}-new-pol"
    skill_name = f"imp-{s}-new-skill"
    cat_name = f"imp-{s}-new-cat"

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name, category=cat_name,
                                             content=_wf_yaml((skill_name,))),
        f"policies/{wf_name}/{pol_name}.yaml": _pol_doc(pol_name),
        f"skills/{skill_name}/SKILL.md": _md(f"name: {skill_name}"),
        f"skills/{skill_name}/references/a.md": b"ref a",
    })
    decisions = {
        "workflows": {wf_name: "import"},
        "skills": {skill_name: "import"},
        "policies": {f"{wf_name} :: {pol_name}": "import"},
    }
    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"imported": 3, "overwritten": 0,
                               "skipped": 0, "errors": 0}
    kinds = {row["kind"]: row for row in body["results"]}
    assert kinds["workflow"]["status"] == "imported"
    assert kinds["policy"]["status"] == "imported"
    assert kinds["skill"]["status"] == "imported"

    wf = await _platform_workflow(wf_name)
    assert wf is not None
    assert wf.is_active is False
    assert wf.yaml_content == _wf_yaml((skill_name,))
    cat_id = wf.workflow_category_id
    assert cat_id is not None
    async with _sm() as session:
        cat = await session.get(WorkflowCategory, cat_id)
        assert cat.name == cat_name

        pol = (await session.execute(select(Policy).where(
            Policy.workflow_id == wf.id))).scalars().all()
        assert len(pol) == 1
        assert pol[0].name == pol_name
        assert pol[0].is_active is False
        assert pol[0].project_id is None

        skill = (await session.execute(select(Skill).where(
            Skill.name == skill_name,
            Skill.project_id.is_(None)))).scalar_one()
        files = (await session.execute(select(SkillFile).where(
            SkillFile.skill_id == skill.id))).scalars().all()
        assert {f.path: f.content for f in files} == {
            "SKILL.md": f"---\nname: {skill_name}\n---\n\n",
            "references/a.md": "ref a",
        }


async def test_import_then_reimport_errors_all_rows(client, world):
    """Second pass with 'import' again: every row already exists — per-row
    error rows, the batch reports errors, nothing is rewritten."""
    s = world["suffix"]
    wf_name = f"imp-{s}-re-wf"
    pol_name = f"imp-{s}-re-pol"
    skill_name = f"imp-{s}-re-skill"

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name,
                                             content=_wf_yaml((skill_name,))),
        f"policies/{wf_name}/{pol_name}.yaml": _pol_doc(pol_name),
        f"skills/{skill_name}/SKILL.md": _md(f"name: {skill_name}"),
    })
    decisions = {
        "workflows": {wf_name: "import"},
        "skills": {skill_name: "import"},
        "policies": {f"{wf_name} :: {pol_name}": "import"},
    }
    assert _do_import(client, zip_bytes, decisions).status_code == 200

    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"imported": 0, "overwritten": 0,
                               "skipped": 0, "errors": 3}
    for row in body["results"]:
        assert row["status"] == "error"
        assert "already exists" in row["message"]
        assert "Overwrite" in row["message"]


async def test_overwrite_updates_yaml_keeps_is_active(client, world):
    """Overwrite replaces content but never deactivates a live workflow or
    policy, and reuses the existing rows."""
    s = world["suffix"]
    wf_name = f"imp-{s}-ovr-wf"
    pol_name = f"imp-{s}-ovr-pol"
    skill_name = f"imp-{s}-ovr-skill"
    old_yaml = _wf_yaml() + "\n"
    new_yaml = _wf_yaml((skill_name,)) + "\n"

    async with _sm() as session:
        wf = Workflow(name=wf_name, version=1, description="old",
                      yaml_content=old_yaml, is_active=True)
        session.add(wf)
        await session.flush()
        pol = Policy(workflow_id=wf.id, name=pol_name, version=1,
                     yaml_content="policy: old\n", is_active=True)
        session.add(pol)
        skill = Skill(name=skill_name, description="old", model="medium")
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md",
                              content="# old skill"))
        await session.commit()
        wf_id, pol_id, skill_id = wf.id, pol.id, skill.id

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name, content=new_yaml),
        f"policies/{wf_name}/{pol_name}.yaml": _pol_doc(pol_name,
                                                        content="policy: new\n"),
        f"skills/{skill_name}/SKILL.md": _md(f"name: {skill_name}",
                                             "# new skill"),
    })
    decisions = {
        "workflows": {wf_name: "overwrite"},
        "skills": {skill_name: "overwrite"},
        "policies": {f"{wf_name} :: {pol_name}": "overwrite"},
    }
    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"imported": 0, "overwritten": 3,
                               "skipped": 0, "errors": 0}

    wf = await _platform_workflow(wf_name)
    assert wf.id == wf_id
    assert wf.yaml_content == new_yaml
    assert wf.is_active is True  # overwriting never deactivates

    async with _sm() as session:
        pol = (await session.execute(select(Policy).where(
            Policy.workflow_id == wf_id))).scalar_one()
        assert pol.id == pol_id
        assert pol.yaml_content == "policy: new\n"
        assert pol.is_active is True

        skill = await session.get(Skill, skill_id)
        assert (await _skill_files(skill.id)) == {
            "SKILL.md": f"---\nname: {skill_name}\n---\n# new skill\n",
        }


async def test_project_import_creates_shadow_skill(client, world):
    """Project scope: a skill existing only as a platform skill analyzes as
    exists=False/platform_exists=True, and importing it creates a PROJECT
    copy that shadows the platform row — the platform row is untouched."""
    s = world["suffix"]
    pid = world["project_id"]
    shared = f"imp-{s}-shadow"
    wf_name = f"imp-{s}-shadow-wf"

    async with _sm() as session:
        skill = Skill(name=shared, description="platform", model="medium")
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md",
                              content="# platform body\n"))
        await session.commit()

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name,
                                             content=_wf_yaml((shared,))),
        f"skills/{shared}/SKILL.md": _md(f"name: {shared}", "# project body"),
    })
    analysis = _analyze(client, zip_bytes, project_id=pid).json()
    assert analysis["skills"][0]["exists"] is False
    assert analysis["skills"][0]["platform_exists"] is True

    decisions = {
        "workflows": {wf_name: "import"},
        "skills": {shared: "import"},
        "policies": {},
    }
    resp = _do_import(client, zip_bytes, decisions, project_id=pid)
    assert resp.status_code == 200
    assert resp.json()["summary"]["imported"] == 2

    async with _sm() as session:
        proj_skill = (await session.execute(select(Skill).where(
            Skill.name == shared, Skill.project_id == pid))).scalar_one()
        assert (await _skill_files(proj_skill.id)) == {
            "SKILL.md": f"---\nname: {shared}\n---\n# project body\n",
        }
        plat_skill = (await session.execute(select(Skill).where(
            Skill.name == shared, Skill.project_id.is_(None)))).scalar_one()
        assert (await _skill_files(plat_skill.id)) == {
            "SKILL.md": "# platform body\n",
        }
        wf = (await session.execute(select(Workflow).where(
            Workflow.name == wf_name, Workflow.project_id == pid))).scalar_one()
        assert wf.project_id == pid


async def test_policy_error_when_workflow_skipped(client, world):
    """A policy decision can't attach when its workflow is skipped AND no
    workflow row exists in scope."""
    s = world["suffix"]
    wf_name = f"imp-{s}-skw-wf"
    pol_name = f"imp-{s}-skw-pol"

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name),
        f"policies/{wf_name}/{pol_name}.yaml": _pol_doc(pol_name),
    })
    decisions = {
        "workflows": {wf_name: "skip"},
        "skills": {},
        "policies": {f"{wf_name} :: {pol_name}": "import"},
    }
    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"imported": 0, "overwritten": 0,
                               "skipped": 1, "errors": 1}
    pol_row = next(r for r in body["results"] if r["kind"] == "policy")
    assert pol_row["status"] == "error"
    assert "not imported" in pol_row["message"]
    assert await _platform_workflow(wf_name) is None


@pytest.mark.parametrize("decisions,detail", [
    ({"workflows": {}, "skills": {}, "policies": {}}, "missing"),
    ({"workflows": {"dec-wf": "import", "ghost": "skip"},
      "skills": {}, "policies": {}}, "unknown"),
    # policy present in the zip but undecided
    ({"workflows": {"dec-wf": "import"},
      "skills": {}, "policies": {}}, "missing"),
    ({"workflows": {"dec-wf": "import"}, "skills": {},
      "policies": {"dec-wf :: dec-pol": "import", "ghost": "skip"}}, "unknown"),
    ({"workflows": {"dec-wf": "explode"}, "skills": {},
      "policies": {"dec-wf :: dec-pol": "import"}}, "invalid decision action"),
    ("not json", "must be a JSON object"),
])
async def test_import_rejects_mismatched_decisions(client, world, decisions, detail):
    """Coverage check fails before ANY row is written — fixed names are safe
    (nothing ever persists from these zips)."""
    zip_bytes = _zip({
        "workflows/dec-wf.yaml": _wf_doc("dec-wf"),
        "policies/dec-wf/dec-pol.yaml": _pol_doc("dec-pol"),
    })
    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]
    assert await _platform_workflow("dec-wf") is None


async def test_import_revalidates_zip_and_catches_phase_change(client, world):
    """Zip changed between phases: decisions match the OLD zip's names."""
    zip_v1 = _zip({
        "workflows/phase-v1.yaml": _wf_doc("phase-v1"),
    })
    zip_v2 = _zip({
        "workflows/phase-v2.yaml": _wf_doc("phase-v2"),
    })
    assert _analyze(client, zip_v1).status_code == 200
    resp = _do_import(client, zip_v2, {
        "workflows": {"phase-v1": "import"}, "skills": {}, "policies": {},
    })
    assert resp.status_code == 422
    assert "unknown" in resp.json()["detail"]


async def test_orphan_policy_in_zip_is_analysis_only(client, world):
    """A policy whose workflow slug matches nothing is an analyze-table row,
    never an import row — the workflow still imports."""
    s = world["suffix"]
    wf_name = f"imp-{s}-orphan-wf"

    zip_bytes = _zip({
        f"workflows/{wf_name}.yaml": _wf_doc(wf_name),
        "policies/ghost/g.yaml": _pol_doc("Ghost"),
    })
    analysis = _analyze(client, zip_bytes).json()
    assert analysis["orphan_policies"] == ["policies/ghost/g.yaml"]
    assert analysis["workflows"][0]["policy_names"] == []

    resp = _do_import(client, zip_bytes, {
        "workflows": {wf_name: "import"}, "skills": {}, "policies": {},
    })
    assert resp.status_code == 200
    assert resp.json()["summary"]["imported"] == 1
    assert await _platform_workflow(wf_name) is not None
