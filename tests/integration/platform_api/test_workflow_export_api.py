"""Integration — admin workflow zip export: platform + project scope.

Exports a zip that round-trips through the workflow import analyze endpoint:
one ``workflows/<slug>.yaml`` manifest per workflow, its policies under
``policies/<slug>/``, and every referenced skill under ``skills/`` — project
skills shadowing platform skills of the same name (the engine resolves the
same way at run init). All names carry a unique suffix so they never collide
with the seeded catalog.

Admin gate: the dev user is promoted to platform ADMIN per test and restored
in the world fixture teardown (same idiom as test_skill_import_api.py).
"""

import io
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

    loop_scope="session": the module engine's asyncpg pool lives on the
    session loop, so fixture DB access must too.

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
        project = Project(name=f"exp-{suffix}-project", description="",
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


def _wf_yaml(skills=()) -> str:
    steps = "\n".join(
        f"  - id: s{i}\n    skill: {s}\n" for i, s in enumerate(skills)
    )
    return f"workflow: wf\nversion: 1\nstart: s0\nsteps:\n{steps}"


async def _make_skill(name: str, content: str, project_id=None):
    async with _sm() as session:
        skill = Skill(name=name, description="desc", model="medium",
                      project_id=project_id)
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md", content=content))
        await session.commit()
        return skill.id


async def _make_workflow(name: str, *, yaml_content, project_id=None,
                         category_id=None, is_active=True) -> uuid.UUID:
    async with _sm() as session:
        wf = Workflow(name=name, version=1, description="",
                      yaml_content=yaml_content, is_active=is_active,
                      project_id=project_id,
                      workflow_category_id=category_id)
        session.add(wf)
        await session.commit()
        return wf.id


async def _make_category(name: str) -> uuid.UUID:
    async with _sm() as session:
        cat = WorkflowCategory(name=name, description="")
        session.add(cat)
        await session.commit()
        return cat.id


def _export(client, workflow_ids, project_id=None):
    body = {"workflow_ids": [str(w) for w in workflow_ids]}
    if project_id is not None:
        body["project_id"] = str(project_id)
    return client.post("/api/admin/workflows/export", json=body)


def _unzip(data: bytes) -> dict[str, str]:
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            out[info.filename] = zf.read(info).decode()
    return out


# ── export ───────────────────────────────────────────────────────────────────


async def test_export_platform_zip_layout_and_round_trip(client, world):
    s = world["suffix"]
    skill_name = f"exp-{s}-design"
    wf_name = f"exp-{s}-delivery"
    cat_name = f"exp-{s}-cat"
    pol_name = f"exp-{s}-strict"
    wf_yaml = _wf_yaml((skill_name,)) + "\n"
    pol_yaml = f"policy: {pol_name}\nversion: 1\ngates: {{}}\n"

    await _make_skill(skill_name, "# design skill\n")
    cat_id = await _make_category(cat_name)
    wf_id = await _make_workflow(wf_name, yaml_content=wf_yaml, category_id=cat_id)
    async with _sm() as session:
        pol = Policy(workflow_id=wf_id, name=pol_name, version=1,
                     yaml_content=pol_yaml, is_active=True)
        session.add(pol)
        await session.commit()

    resp = _export(client, [wf_id])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="bheembhai-workflows-')

    tree = _unzip(resp.content)
    slug = wf_name  # already slug-safe (hex + hyphens)
    assert list(tree) == [
        f"workflows/{slug}.yaml",
        f"policies/{slug}/{pol_name}.yaml",
        f"skills/{skill_name}/SKILL.md",
    ]

    manifest = yaml.safe_load(tree[f"workflows/{slug}.yaml"])
    assert manifest["name"] == wf_name
    assert manifest["version"] == 1
    assert manifest["category"] == cat_name
    assert manifest["is_active"] is True
    assert manifest["content"] == wf_yaml

    pol_doc = yaml.safe_load(tree[f"policies/{slug}/{pol_name}.yaml"])
    assert (pol_doc["name"], pol_doc["version"]) == (pol_name, 1)
    assert pol_doc["content"] == pol_yaml

    assert tree[f"skills/{skill_name}/SKILL.md"] == "# design skill\n"

    # Round-trip: the exported zip must analyze with exists flags all True.
    analyze = client.post(
        "/api/admin/workflows/import/analyze",
        files={"zip_file": ("workflows.zip", resp.content, "application/zip")},
    )
    assert analyze.status_code == 200
    body = analyze.json()
    assert body["workflows"][0]["name"] == wf_name
    assert body["workflows"][0]["exists"] is True
    assert body["workflows"][0]["referenced_skills"] == [skill_name]
    assert body["skills"][0]["exists"] is True
    assert body["policies"][0]["exists"] is True
    assert body["missing_skills"] == []
    assert body["orphan_policies"] == []


async def test_export_project_scope_shadows_platform_skill(client, world):
    """Project skills win over platform skills of the same name — the export
    zip carries the project's content. Scoped export is strict (an unknown
    project 404s), while the unscoped export used by the platform list page
    accepts project rows and still resolves their skills in the project
    scope."""
    s = world["suffix"]
    pid = world["project_id"]
    shared = f"exp-{s}-shared"
    wf_name = f"exp-{s}-proj-wf"

    await _make_skill(shared, "# platform body\n")
    await _make_skill(shared, "# project body\n", project_id=pid)
    wf_id = await _make_workflow(
        wf_name, yaml_content=_wf_yaml((shared,)), project_id=pid)

    resp = _export(client, [wf_id], project_id=pid)
    assert resp.status_code == 200
    tree = _unzip(resp.content)
    assert tree[f"skills/{shared}/SKILL.md"] == "# project body\n"
    assert next(iter(tree)) == f"workflows/{wf_name}.yaml"

    # Unscoped (platform page): the same project row exports fine and still
    # ships the project's shadow content — the workflow's OWN scope rules.
    resp = _export(client, [wf_id])
    assert resp.status_code == 200
    tree = _unzip(resp.content)
    assert tree[f"skills/{shared}/SKILL.md"] == "# project body\n"

    # Same workflow id under an unknown project → 404 on the scope itself.
    resp = _export(client, [wf_id], project_id=uuid.uuid4())
    assert resp.status_code == 404


async def test_export_unknown_workflow_or_project_404(client, world):
    s = world["suffix"]
    wf_id = await _make_workflow(
        f"exp-{s}-plat", yaml_content=_wf_yaml())

    resp = _export(client, [uuid.uuid4()])
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]

    resp = _export(client, [wf_id], project_id=uuid.uuid4())
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


async def test_export_skill_without_skill_md_422(client, world):
    """A referenced skill with no SKILL.md cannot be zipped — 422 before any
    bytes are produced."""
    s = world["suffix"]
    skill_name = f"exp-{s}-nofiles"
    wf_name = f"exp-{s}-nofiles-wf"
    async with _sm() as session:
        session.add(Skill(name=skill_name, description="desc", model="medium"))
        await session.commit()
    wf_id = await _make_workflow(
        wf_name, yaml_content=_wf_yaml((skill_name,)))

    resp = _export(client, [wf_id])
    assert resp.status_code == 422
    assert "no SKILL.md" in resp.json()["detail"]


# ── mixed-scope selection (the platform list page shows every scope) ──────


async def test_export_unscoped_mixed_selection_uses_each_workflows_own_scope(client, world):
    """The platform workflows page lists platform and project rows in one
    table. An unscoped export of a mixed selection must succeed and resolve
    each workflow's skills in ITS OWN scope — a project workflow's
    project-only skill ships, and a shared name falls back to the platform
    copy exactly like run init."""
    s = world["suffix"]
    pid = world["project_id"]
    shared = f"exp-{s}-mixed-shared"
    proj_only = f"exp-{s}-mixed-projonly"
    plat_name = f"exp-{s}-mixed-plat"
    proj_name = f"exp-{s}-mixed-proj"

    await _make_skill(shared, "# platform body\n")  # platform only — no shadow
    await _make_skill(proj_only, "# only in project\n", project_id=pid)
    plat_id = await _make_workflow(
        plat_name, yaml_content=_wf_yaml((shared,)))
    proj_id = await _make_workflow(
        proj_name, yaml_content=_wf_yaml((shared, proj_only)), project_id=pid)

    resp = _export(client, [plat_id, proj_id])
    assert resp.status_code == 200
    tree = _unzip(resp.content)
    assert f"workflows/{plat_name}.yaml" in tree
    assert f"workflows/{proj_name}.yaml" in tree
    assert tree[f"skills/{shared}/SKILL.md"] == "# platform body\n"
    # The project-only skill proves resolution happened in the WORKFLOW's
    # scope — platform scope (the old behavior) would never find it.
    assert tree[f"skills/{proj_only}/SKILL.md"] == "# only in project\n"


async def test_export_cross_scope_same_name_version_422(client, world):
    """A workflow shadowing a platform workflow of the same name+version
    cannot share one zip entry — 422 telling the user to export per scope."""
    s = world["suffix"]
    pid = world["project_id"]
    name = f"exp-{s}-dup"

    plat_id = await _make_workflow(name, yaml_content=_wf_yaml())
    proj_id = await _make_workflow(name, yaml_content=_wf_yaml(), project_id=pid)

    resp = _export(client, [plat_id, proj_id])
    assert resp.status_code == 422
    assert "more than one scope" in resp.json()["detail"]
    assert name in resp.json()["detail"]


async def test_export_cross_scope_skill_divergence_422(client, world):
    """Two workflows referencing the same skill name where their scopes
    resolve DIFFERENT content (project shadow vs platform copy) — 422
    instead of silently shipping one scope's content."""
    s = world["suffix"]
    pid = world["project_id"]
    shared = f"exp-{s}-div"

    await _make_skill(shared, "# platform body\n")
    await _make_skill(shared, "# project body\n", project_id=pid)
    plat_id = await _make_workflow(
        f"exp-{s}-div-plat", yaml_content=_wf_yaml((shared,)))
    proj_id = await _make_workflow(
        f"exp-{s}-div-proj", yaml_content=_wf_yaml((shared,)), project_id=pid)

    resp = _export(client, [plat_id, proj_id])
    assert resp.status_code == 422
    assert "resolves to different content" in resp.json()["detail"]
    assert shared in resp.json()["detail"]
