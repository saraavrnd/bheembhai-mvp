"""Unit — run-scoped skill library: DB shadowing, no filesystem.

Covers the two pure pieces of ``engine_service/skills.py`` with no database:
the duck-typed session seam and project-shadows-platform resolution. (Phase 1
dropped the host materializer — skills now reach the agent as S3 bundles; the
publishing side is covered in tests/unit/shared/test_skill_publish.py.)
"""

import uuid

from bheembhai.models.skill import Skill

from engine_service.skills import (
    effective_skill_map,
    load_run_skills,
)

# ── fixtures / builders ──────────────────────────────────────────────────────


def _skill(name: str, project_id=None) -> Skill:
    return Skill(name=name, project_id=project_id, description=f"desc {name}")


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
