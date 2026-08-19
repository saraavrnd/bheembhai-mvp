"""Integration — admin skill zip export over real HTTP + Postgres.

The mirror of the import module: create platform skills (directly in the DB,
or through the import endpoint for the deployability round-trip), POST the
export selection, and assert the downloaded zip is import-compatible. All
skill names are suffixed so they never collide with the seeded catalog.

Admin gate: the dev user is promoted to platform ADMIN per test and restored
in the world fixture teardown (same idiom as test_admin_project_delete.py).
"""

import io
import json
import re
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
            Skill.name.like(f"exp-{suffix}-%")))
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


def _export(client, names):
    return client.post(
        "/api/admin/skills/export",
        json={"names": names},
    )


async def _create_skill(name: str, files: dict[str, str],
                        model: str = "medium") -> str:
    async with _sm() as session:
        skill = Skill(name=name, description="", model=model)
        session.add(skill)
        await session.flush()
        session.add_all([
            SkillFile(skill_id=skill.id, path=path, content=content)
            for path, content in files.items()
        ])
        await session.commit()
        return skill.id


async def _platform_skill(name: str) -> Skill | None:
    async with _sm() as session:
        return (await session.execute(select(Skill).where(
            Skill.name == name, Skill.project_id.is_(None)))).scalar_one_or_none()


async def _skill_files(skill_id) -> dict[str, str]:
    async with _sm() as session:
        rows = (await session.execute(select(SkillFile).where(
            SkillFile.skill_id == skill_id))).scalars().all()
        return {row.path: row.content for row in rows}


# ── export phase ─────────────────────────────────────────────────────────────


async def test_export_is_admin_gated(client, world):
    """Non-ADMIN → 403 (world promotes to ADMIN; demote here explicitly)."""
    async with _sm() as session:
        user = (await session.execute(select(User).where(
            User.external_id == DEV_USER[0],
            User.auth_provider == DEV_USER[1]))).scalar_one()
        user.platform_role = "USER"
        await session.commit()

    resp = _export(client, [f"exp-{world['suffix']}-x"])
    assert resp.status_code == 403


async def test_export_returns_downloadable_import_compatible_zip(client, world):
    name = f"exp-{world['suffix']}-one"
    await _create_skill(name, {
        "SKILL.md": f"---\nname: {name}\n---\n# body\n",
        "references/a.md": "ref a",
        "notes/empty.txt": "",
    })

    resp = _export(client, [name])
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert re.fullmatch(
        r'attachment; filename="bheembhai-skills-\d{8}\.zip"',
        resp.headers["content-disposition"],
    )
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.namelist() == [
            f"skills/{name}/SKILL.md",
            f"skills/{name}/notes/empty.txt",
            f"skills/{name}/references/a.md",
        ]
        assert zf.read(f"skills/{name}/references/a.md") == b"ref a"


async def test_export_unknown_name_404(client, world):
    resp = _export(client, [f"exp-{world['suffix']}-ghost"])
    assert resp.status_code == 404
    assert f"exp-{world['suffix']}-ghost" in resp.json()["detail"]


async def test_export_empty_names_422(client, world):
    resp = _export(client, [])
    assert resp.status_code == 422


async def test_export_skill_without_skill_md_422(client, world):
    name = f"exp-{world['suffix']}-nomd"
    await _create_skill(name, {"references/x.md": "x"})
    resp = _export(client, [name])
    assert resp.status_code == 422
    assert "has no SKILL.md" in resp.json()["detail"]


async def test_export_unsafe_path_422(client, world):
    name = f"exp-{world['suffix']}-slip"
    await _create_skill(name, {
        "SKILL.md": f"---\nname: {name}\n---\n",
        "../evil.md": "x",
    })
    resp = _export(client, [name])
    assert resp.status_code == 422
    assert "escapes the skill directory" in resp.json()["detail"]


async def test_export_per_file_budget_422(client, world, monkeypatch):
    from platform_api.routers import admin as admin_router
    monkeypatch.setattr(admin_router, "MAX_SINGLE_FILE_BYTES", 10)
    name = f"exp-{world['suffix']}-big"
    await _create_skill(name, {"SKILL.md": f"---\nname: {name}\n---\n# big\n"})
    resp = _export(client, [name])
    assert resp.status_code == 422
    assert "file too large" in resp.json()["detail"]


async def test_export_round_trip_is_deployable(client, world):
    """Import a zip (external ref + empty file + unicode name) → export →
    analyze the exported bytes → identical rows. This is the deployability
    guarantee: what the import feature stores is exactly what export ships."""
    name = f"exp-{world['suffix']}-campañas"
    zip_bytes = _zip({
        f"skills/{name}/SKILL.md": _md(
            f"name: {name}", "[r](../../tools/REGISTRY.md)"),
        f"skills/{name}/notes/empty.txt": b"",
        "tools/REGISTRY.md": b"# registry",
    })
    imp = client.post(
        "/api/admin/skills/import",
        files={"zip_file": ("skills.zip", zip_bytes, "application/zip")},
        data={"decisions": json.dumps({name: "import"})},
    )
    assert imp.status_code == 200
    assert imp.json()["results"][0]["status"] == "imported"

    skill = await _platform_skill(name)
    assert skill is not None
    stored = await _skill_files(skill.id)

    resp = _export(client, [name])
    assert resp.status_code == 200

    from platform_api.skill_import import analyze_zip
    bundle = analyze_zip(resp.content).skills[0]
    assert bundle.name == name
    assert bundle.files == stored  # what went in is what comes out
    assert bundle.missing_referenced == []
    assert "tools/REGISTRY.md" in bundle.files  # dependency came along
    assert "../../tools/" not in bundle.files["SKILL.md"]
