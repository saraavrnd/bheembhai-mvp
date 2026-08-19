"""Integration — admin skill zip import: two-phase flow over real HTTP +
Postgres.

Analyze (stateless) returns the analysis table with exists flags; confirm
re-uploads the same zip plus per-skill decisions and writes platform
Skill/SkillFile rows via upsert_skill. All skill names are suffixed so they
never collide with the seeded catalog (platform rows are unique on name).

Admin gate: the dev user is promoted to platform ADMIN per test and restored
in the world fixture teardown (same idiom as test_admin_project_delete.py).
"""

import io
import json
import uuid
import zipfile

import pytest
import pytest_asyncio
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import User
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
    """ADMIN dev user for the require_admin gate + a unique name suffix.

    loop_scope="session": the module engine's asyncpg pool lives on the
    session loop, so fixture DB access must too.

    Teardown deletes every platform skill created under the suffix and
    restores the dev user's previous role.
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
        await session.commit()

    yield {"suffix": suffix, "prev_role": prev_role}

    # Role restore FIRST — a failed skill delete must never leave the shared
    # dev user stuck as ADMIN (that breaks PM-gated suites).
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one()
        user.platform_role = prev_role
        await session.execute(delete(Skill).where(
            Skill.project_id.is_(None),
            Skill.name.like(f"imp-{suffix}-%")))
        await session.commit()


# ── helpers ─────────────────────────────────────────────────────────────────


def _zip(entries: dict[str, bytes], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _md(frontmatter: str, body: str = "") -> bytes:
    return f"---\n{frontmatter}\n---\n{body}\n".encode()


def _analyze(client, zip_bytes):
    return client.post(
        "/api/admin/skills/import/analyze",
        files={"zip_file": ("skills.zip", zip_bytes, "application/zip")},
    )


def _do_import(client, zip_bytes, decisions: dict[str, str]):
    return client.post(
        "/api/admin/skills/import",
        files={"zip_file": ("skills.zip", zip_bytes, "application/zip")},
        data={"decisions": json.dumps(decisions)},
    )


async def _platform_skill(name: str) -> Skill | None:
    async with _sm() as session:
        return (await session.execute(select(Skill).where(
            Skill.name == name, Skill.project_id.is_(None)))).scalar_one_or_none()


async def _skill_files(skill_id) -> dict[str, str]:
    async with _sm() as session:
        rows = (await session.execute(select(SkillFile).where(
            SkillFile.skill_id == skill_id))).scalars().all()
        return {row.path: row.content for row in rows}


# ── analyze phase ────────────────────────────────────────────────────────────


async def test_import_page_route_renders(client):
    """/admin/skills/import must not be swallowed by /admin/skills/{skill_id}."""
    resp = client.get("/admin/skills/import")
    assert resp.status_code == 200
    assert "upload-card" in resp.text
    assert "analyzeZip" in resp.text


async def test_import_endpoints_are_admin_gated(client, world):
    """Non-ADMIN → 403 (world promotes to ADMIN; demote here explicitly)."""
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one()
        user.platform_role = "USER"
        await session.commit()

    resp = _analyze(client, _zip({"skills/a/SKILL.md": _md("name: a")}))
    assert resp.status_code == 403


async def test_analyze_returns_table_with_exists_flags_and_tier_mapping(client, world):
    collide = f"imp-{world['suffix']}-collide"
    fresh = f"imp-{world['suffix']}-fresh"
    async with _sm() as session:
        session.add(Skill(name=collide, description="pre-existing",
                          model="medium"))
        await session.commit()

    zip_bytes = _zip({
        f"skills/{collide}/SKILL.md": _md(f"name: {collide}\nmodel: opus"),
        f"skills/{fresh}/SKILL.md": _md(
            f"name: {fresh}\nmodel: haiku",
            "[see](references/ctx.md) [tools](../../tools/REGISTRY.md)"),
        f"skills/{fresh}/references/ctx.md": b"context",
        "tools/REGISTRY.md": b"# registry",
    })
    resp = _analyze(client, zip_bytes)
    assert resp.status_code == 200
    body = resp.json()
    by_name = {row["name"]: row for row in body["skills"]}
    assert body["invalid_dirs"] == []
    assert body["other_entries"] == ["tools/REGISTRY.md"]
    assert by_name[collide]["exists"] is True
    assert by_name[collide]["model"] == "high"  # opus → high
    assert by_name[fresh]["exists"] is False
    assert by_name[fresh]["model"] == "low"  # haiku → low
    # files/file_contents cover the whole import set: the skill's own files
    # plus zip-backed refs outside the skill dir (tagged external in the UI).
    assert by_name[fresh]["files"] == [
        "SKILL.md", "references/ctx.md", "tools/REGISTRY.md"]
    assert by_name[fresh]["missing_referenced"] == []
    # tools/REGISTRY.md is in the zip but outside the skill dir → satisfied,
    # reported as external, not as missing.
    assert by_name[fresh]["external_references"] == ["tools/REGISTRY.md"]
    # file_contents powers the dependency-files preview drawer.
    assert by_name[fresh]["file_contents"]["references/ctx.md"] == "context"
    assert by_name[fresh]["file_contents"]["tools/REGISTRY.md"] == "# registry"
    assert "model: haiku" in by_name[fresh]["file_contents"]["SKILL.md"]
    assert by_name[collide]["file_contents"] == {
        "SKILL.md": f"---\nname: {collide}\nmodel: opus\n---\n\n"}


async def test_analyze_rejects_zip_without_skills_folder(client, world):
    resp = _analyze(client, _zip({"tools/helper.py": b"print(1)"}))
    assert resp.status_code == 422
    assert "no skills/ folder" in resp.json()["detail"]


async def test_analyze_rejects_oversized_upload(client, world, monkeypatch):
    from platform_api.routers import admin as admin_router
    monkeypatch.setattr(admin_router, "MAX_UPLOAD_BYTES", 512)
    # ZIP_STORED: 'x'*600 would deflate to almost nothing under the cap.
    zip_bytes = _zip(
        {"skills/a/SKILL.md": _md("name: a") + b"x" * 600},
        compression=zipfile.ZIP_STORED,
    )
    resp = _analyze(client, zip_bytes)
    assert resp.status_code == 413
    assert "MB limit" in resp.json()["detail"]


async def test_analyze_flags_skill_dir_without_skill_md(client, world):
    zip_bytes = _zip({
        "skills/ok/SKILL.md": _md("name: ok"),
        "skills/no-md/readme.txt": b"no SKILL.md",
    })
    resp = _analyze(client, zip_bytes)
    assert resp.status_code == 200
    body = resp.json()
    assert [row["name"] for row in body["skills"]] == ["ok"]
    assert body["invalid_dirs"] == ["skills/no-md"]


# ── import phase ─────────────────────────────────────────────────────────────


async def test_import_creates_platform_rows_with_files(client, world):
    name = f"imp-{world['suffix']}-new"
    zip_bytes = _zip({
        f"skills/{name}/SKILL.md": _md(f"name: {name}\nmodel: opus", "# body"),
        f"skills/{name}/references/a.md": b"ref a",
        f"skills/{name}/notes/empty.txt": b"",  # empty files round-trip
    })
    resp = _do_import(client, zip_bytes, {name: "import"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == {"imported": 1, "overwritten": 0,
                               "skipped": 0, "errors": 0}
    assert body["results"][0]["status"] == "imported"

    skill = await _platform_skill(name)
    assert skill is not None
    assert skill.project_id is None
    assert skill.model == "high"  # opus → high through the endpoint
    files = await _skill_files(skill.id)
    assert files == {
        "SKILL.md": "---\nname: " + name + "\nmodel: opus\n---\n# body\n",
        "references/a.md": "ref a",
        "notes/empty.txt": "",
    }


async def test_overwrite_replaces_files_and_deletes_stale(client, world):
    name = f"imp-{world['suffix']}-ovr"
    async with _sm() as session:
        skill = Skill(name=name, description="old desc", model="medium")
        session.add(skill)
        await session.flush()
        session.add_all([
            SkillFile(skill_id=skill.id, path="SKILL.md", content="# old skill"),
            SkillFile(skill_id=skill.id, path="references/old.md",
                      content="stale"),
        ])
        await session.commit()
        skill_id = skill.id

    zip_bytes = _zip({
        f"skills/{name}/SKILL.md": _md(f"name: {name}\nmodel: haiku",
                                       "# new skill"),
        f"skills/{name}/references/new.md": b"fresh",
    })
    resp = _do_import(client, zip_bytes, {name: "overwrite"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "overwritten"

    skill = await _platform_skill(name)
    assert skill.id == skill_id  # same row, not re-created
    assert skill.model == "low"  # haiku → low
    assert skill.description == ""
    assert await _skill_files(skill.id) == {
        "SKILL.md": "---\nname: " + name + "\nmodel: haiku\n---\n# new skill\n",
        "references/new.md": "fresh",
    }  # references/old.md deleted


async def test_import_brings_external_referenced_files_into_the_skill(client, world):
    """Zip-backed refs outside the skill dir come along with the skill and
    the SKILL.md reference is rewritten to the new in-skill location."""
    name = f"imp-{world['suffix']}-ext"
    zip_bytes = _zip({
        f"skills/{name}/SKILL.md": _md(
            f"name: {name}",
            "[registry](../../tools/REGISTRY.md) "
            "[guide](../../tools/integrations/ga.md)"),
        "tools/REGISTRY.md": b"# registry",
        "tools/integrations/ga.md": b"# ga guide",
    })
    resp = _do_import(client, zip_bytes, {name: "import"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "imported"
    assert "2 referenced" in body["results"][0]["message"]

    skill = await _platform_skill(name)
    files = await _skill_files(skill.id)
    assert files["tools/REGISTRY.md"] == "# registry"
    assert files["tools/integrations/ga.md"] == "# ga guide"
    assert "[registry](tools/REGISTRY.md)" in files["SKILL.md"]
    assert "[guide](tools/integrations/ga.md)" in files["SKILL.md"]
    assert "../../tools/" not in files["SKILL.md"]


async def test_skip_leaves_skill_untouched(client, world):
    name = f"imp-{world['suffix']}-skip"
    async with _sm() as session:
        skill = Skill(name=name, description="keep me", model="high")
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md",
                              content="# original"))
        await session.commit()

    zip_bytes = _zip({f"skills/{name}/SKILL.md": _md(
        f"name: {name}\nmodel: low", "# replacement")})
    resp = _do_import(client, zip_bytes, {name: "skip"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "skipped"
    assert body["summary"]["skipped"] == 1

    skill = await _platform_skill(name)
    assert skill.description == "keep me"
    assert skill.model == "high"
    assert await _skill_files(skill.id) == {"SKILL.md": "# original"}


async def test_import_on_existing_errors_but_sibling_imports(client, world):
    exists_name = f"imp-{world['suffix']}-exists"
    fresh_name = f"imp-{world['suffix']}-fresh"
    async with _sm() as session:
        skill = Skill(name=exists_name, description="existing", model="medium")
        session.add(skill)
        await session.flush()
        session.add(SkillFile(skill_id=skill.id, path="SKILL.md",
                              content="# existing"))
        await session.commit()

    zip_bytes = _zip({
        f"skills/{exists_name}/SKILL.md": _md(f"name: {exists_name}"),
        f"skills/{fresh_name}/SKILL.md": _md(f"name: {fresh_name}"),
    })
    resp = _do_import(
        client, zip_bytes, {exists_name: "import", fresh_name: "import"})
    assert resp.status_code == 200
    body = resp.json()
    by_name = {row["name"]: row for row in body["results"]}
    assert by_name[exists_name]["status"] == "error"
    assert "already exists" in by_name[exists_name]["message"]
    assert "Overwrite" in by_name[exists_name]["message"]
    assert by_name[fresh_name]["status"] == "imported"
    assert body["summary"]["errors"] == 1
    assert body["summary"]["imported"] == 1

    existing = await _platform_skill(exists_name)
    assert await _skill_files(existing.id) == {"SKILL.md": "# existing"}
    assert await _platform_skill(fresh_name) is not None


@pytest.mark.parametrize("decisions,detail", [
    ({"only-one": "import"}, "missing"),       # second skill undecided
    ({"a": "import", "b": "import", "ghost": "skip"}, "unknown"),
    ({"a": "explode"}, "invalid decision action"),
])
async def test_import_rejects_mismatched_decisions(client, world, decisions, detail):
    zip_bytes = _zip({
        "skills/a/SKILL.md": _md("name: a"),
        "skills/b/SKILL.md": _md("name: b"),
    })
    resp = _do_import(client, zip_bytes, decisions)
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]


async def test_import_falls_back_to_dirname_without_frontmatter(client, world):
    dirname = f"imp-{world['suffix']}-nofm"
    zip_bytes = _zip({f"skills/{dirname}/SKILL.md": b"# no frontmatter\n"})
    analysis = _analyze(client, zip_bytes).json()
    assert analysis["skills"][0]["name"] == dirname
    assert analysis["skills"][0]["model"] == "medium"

    resp = _do_import(client, zip_bytes, {dirname: "import"})
    assert resp.status_code == 200
    assert resp.json()["results"][0]["status"] == "imported"
    skill = await _platform_skill(dirname)
    assert skill is not None
    assert await _skill_files(skill.id) == {"SKILL.md": "# no frontmatter\n"}


async def test_overlong_name_warns_at_analyze_and_errors_at_import(client, world):
    long_name = "x" * 101
    zip_bytes = _zip({"skills/somedir/SKILL.md": _md(f"name: {long_name}")})

    analysis = _analyze(client, zip_bytes).json()
    assert any("exceeds 100" in w for w in analysis["skills"][0]["warnings"])

    resp = _do_import(client, zip_bytes, {long_name: "import"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"][0]["status"] == "error"
    assert "exceeds 100 characters" in body["results"][0]["message"]
    assert body["summary"]["errors"] == 1
    assert await _platform_skill(long_name) is None


async def test_import_revalidates_zip_and_catches_phase_change(client, world):
    """Zip changed between phases: decisions match the OLD zip's names."""
    zip_v1 = _zip({"skills/v1-skill/SKILL.md": _md("name: v1-skill")})
    zip_v2 = _zip({"skills/v2-skill/SKILL.md": _md("name: v2-skill")})
    assert _analyze(client, zip_v1).status_code == 200
    resp = _do_import(client, zip_v2, {"v1-skill": "import"})
    assert resp.status_code == 422
    assert "unknown" in resp.json()["detail"]
