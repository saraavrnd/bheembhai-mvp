"""Unit — run-scoped skill library: DB shadowing + host materialization.

Covers the three pure pieces of ``engine_service/skills.py`` with no database:
the duck-typed session seam, project-shadows-platform resolution, and the
wipe-then-write filesystem materializer.
"""

import uuid
from pathlib import Path

from bheembhai.models.skill import Skill, SkillFile

from engine_service.skills import (
    effective_skill_map,
    load_run_skills,
    materialize_skills,
)

# ── fixtures / builders ──────────────────────────────────────────────────────


def _skill(name: str, project_id=None) -> Skill:
    return Skill(name=name, project_id=project_id, description=f"desc {name}")


def _file(path: str, content: str) -> SkillFile:
    return SkillFile(skill_id=uuid.uuid4(), path=path, content=content)


class _FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def unique(self):
        return self

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    """Duck-typed session: pops one result-set per execute() call.

    First execute = platform select, second = project select.
    """

    def __init__(self, *result_sets):
        self._pending = list(result_sets)

    async def execute(self, stmt):
        return _FakeResult(self._pending.pop(0))


# ── load_run_skills ──────────────────────────────────────────────────────────


async def test_load_run_skills_splits_platform_and_project():
    platform = [_skill("story-design"), _skill("code-review")]
    project = [_skill("story-design", project_id=uuid.uuid4())]
    session = _FakeSession(platform, project)

    got_project, got_platform = await load_run_skills(session, uuid.uuid4())

    assert {s.name for s in got_platform} == {"story-design", "code-review"}
    assert all(s.project_id is None for s in got_platform)
    assert [s.name for s in got_project] == ["story-design"]
    assert all(s.project_id is not None for s in got_project)


async def test_load_run_skills_none_project_id_skips_project_select():
    session = _FakeSession([_skill("code-review")])
    got_project, got_platform = await load_run_skills(session, None)
    assert got_project == []
    assert [s.name for s in got_platform] == ["code-review"]
    assert session._pending == []  # only one execute happened


# ── effective_skill_map ──────────────────────────────────────────────────────


def test_effective_map_project_shadows_platform():
    platform = [_skill("story-design"), _skill("code-review")]
    project = [_skill("story-design", project_id=uuid.uuid4())]

    by_name = effective_skill_map(project, platform)

    assert set(by_name) == {"story-design", "code-review"}
    assert by_name["story-design"].project_id is not None  # project row wins
    assert by_name["code-review"].project_id is None


def test_effective_map_without_project_skills_is_platform_only():
    platform = [_skill("story-design")]
    by_name = effective_skill_map([], platform)
    assert by_name["story-design"].project_id is None


# ── materialize_skills ───────────────────────────────────────────────────────


def _materialized_tree(skills_dir) -> dict:
    """path relative to the materialized root -> content."""
    out = {}
    root = Path(skills_dir)
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_text()
    return out


def test_materialize_writes_full_library_with_modes(tmp_path):
    workdir = tmp_path / "work"
    run_id = uuid.uuid4()
    skill = _skill("story-design")
    skill.files = [
        _file("SKILL.md", "# story design"),
        _file("references/context.md", "ref content"),
    ]
    root = materialize_skills(workdir, run_id, {"story-design": skill})

    tree = _materialized_tree(str(root))
    assert tree == {
        "story-design/SKILL.md": "# story design",
        "story-design/references/context.md": "ref content",
    }
    # Bind mounts don't inherit the image's chmod — verify explicit modes.
    assert (root / "story-design" / "SKILL.md").stat().st_mode & 0o777 == 0o644
    assert (root / "story-design" / "references").stat().st_mode & 0o777 == 0o755
    assert root.stat().st_mode & 0o777 == 0o755


def test_materialize_wipes_stale_files_on_rerun(tmp_path):
    workdir = tmp_path / "work"
    run_id = uuid.uuid4()
    skill = _skill("story-design")
    skill.files = [_file("SKILL.md", "v1")]

    root = materialize_skills(workdir, run_id, {"story-design": skill})
    # Simulate a PM edit landing between dispatches, plus a stale orphan file.
    skill.files = [_file("SKILL.md", "v2")]
    (root / "story-design" / "orphan.md").write_text("stale")

    root = materialize_skills(workdir, run_id, {"story-design": skill})

    tree = _materialized_tree(str(root))
    assert tree == {"story-design/SKILL.md": "v2"}


def test_materialize_skips_path_escaping_skill_dir(tmp_path):
    workdir = tmp_path / "work"
    run_id = uuid.uuid4()
    skill = _skill("evil")
    skill.files = [
        _file("SKILL.md", "ok"),
        _file("../escaped.md", "must not be written"),
    ]

    root = materialize_skills(workdir, run_id, {"evil": skill})

    tree = _materialized_tree(str(root))
    assert tree == {"evil/SKILL.md": "ok"}
    assert not (root / "escaped.md").exists()


def test_materialize_handles_skill_without_files(tmp_path):
    root = materialize_skills(tmp_path / "work", uuid.uuid4(), {"empty": _skill("empty")})
    assert (root / "empty").is_dir()
    assert not any(p.is_file() for p in (root / "empty").iterdir())


def test_materialize_with_invalid_run_id_type_is_handled_by_str(tmp_path):
    # run_id is always a UUID in production; the function stringifies it.
    root = materialize_skills(tmp_path / "work", "run-abc", {"s": _skill("s")})
    assert root.name == "run-abc"
