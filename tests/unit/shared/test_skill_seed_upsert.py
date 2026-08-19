"""Unit — shared skill helpers: frontmatter parsing + platform skill upsert.

Covers ``parse_skill_frontmatter`` (SKILL.md YAML frontmatter → DB fields,
with the opus/sonnet/haiku tier mapping) and ``upsert_skill`` against a
duck-typed fake session (create/update, file add/update/stale-delete, no
commit, platform-scope filtering). Plus a seed-level regression pinning the
tier fix: ``model: opus`` must land as ``high``, not ``medium``.
"""

import uuid

import pytest
from bheembhai.database import (
    MODEL_TIER_MAP,
    parse_skill_frontmatter,
    seed_default_skills,
    upsert_skill,
)
from bheembhai.models.skill import Skill, SkillFile

# ── parse_skill_frontmatter ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("opus", "high"),
        ("sonnet", "medium"),
        ("haiku", "low"),
        ("high", "high"),
        ("medium", "medium"),
        ("low", "low"),
        ("Opus", "high"),  # case-insensitive
        ("  haiku  ", "low"),  # stripped
    ],
)
def test_frontmatter_maps_model_tiers(raw, expected):
    fm = parse_skill_frontmatter(f"---\nname: s\nmodel: {raw}\n---\nbody", "dir")
    assert fm.model == expected
    assert fm.warnings == []


def test_frontmatter_unknown_model_defaults_medium_with_warning():
    fm = parse_skill_frontmatter("---\nname: s\nmodel: gpt-5\n---\n", "dir")
    assert fm.model == "medium"
    assert fm.warnings == ["unknown model 'gpt-5' — defaulted to medium"]


def test_frontmatter_missing_model_defaults_medium_no_warning():
    fm = parse_skill_frontmatter("---\nname: s\n---\n", "dir")
    assert fm.model == "medium"
    assert fm.warnings == []


def test_frontmatter_without_fence_uses_defaults():
    fm = parse_skill_frontmatter("# Just a heading\n", "my-dir")
    assert (fm.name, fm.description, fm.model, fm.compatibility) == (
        "my-dir", "", "medium", None,
    )
    assert fm.warnings == []


def test_frontmatter_missing_closing_fence_defaults_with_warning():
    fm = parse_skill_frontmatter("---\nname: s\nno closing fence", "my-dir")
    assert fm.name == "my-dir"
    assert fm.warnings == ["SKILL.md frontmatter is missing a closing '---' — using defaults"]


def test_frontmatter_unparseable_yaml_returns_none():
    assert parse_skill_frontmatter("---\nname: [unclosed\n---\n", "dir") is None


@pytest.mark.parametrize("name_value,expected", [
    ("", "dir"),          # empty → fallback
    ("   ", "dir"),       # whitespace-only → fallback
    (123, "123"),         # non-str coerced
    ("real-name", "real-name"),
])
def test_frontmatter_name_fallback_and_coercion(name_value, expected):
    fm = parse_skill_frontmatter(f"---\nname: {name_value}\n---\n", "dir")
    assert fm.name == expected


def test_frontmatter_description_none_becomes_empty():
    fm = parse_skill_frontmatter("---\nname: s\ndescription:\n---\n", "dir")
    assert fm.description == ""


def test_frontmatter_compatibility_coerced():
    fm = parse_skill_frontmatter("---\nname: s\ncompatibility: Tool-agnostic\n---\n", "dir")
    assert fm.compatibility == "Tool-agnostic"


# ── upsert_skill (duck-typed fake session) ───────────────────────────────────


class _FakeScalars:
    def __init__(self, rows):
        self._rows = list(rows)

    def unique(self):
        return self

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows=None, one=None):
        self._rows = rows or []
        self._one = one

    def scalars(self):
        return _FakeScalars(self._rows)

    def scalar_one_or_none(self):
        return self._one


class _RecordingSession:
    """Pops one result-set per execute() call; records writes; never commits."""

    def __init__(self, *result_sets):
        self._pending = list(result_sets)
        self.statements = []
        self.added = []
        self.deleted = []
        self.flushes = 0
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return self._pending.pop(0)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


def _existing_skill(name="story-design"):
    return Skill(name=name, project_id=None, description="old", model="medium")


def _file_row(path, content):
    return SkillFile(skill_id=uuid.uuid4(), path=path, content=content)


async def test_upsert_creates_skill_and_files_without_commit():
    session = _RecordingSession(
        _FakeResult(one=None),   # skill select: no existing row
        _FakeResult(rows=[]),    # file select: no existing files
    )
    skill = await upsert_skill(
        session,
        name="story-design",
        description="desc",
        model="high",
        compatibility=None,
        files={"SKILL.md": "md", "references/a.md": "a"},
    )

    assert session.commits == 0
    assert session.flushes == 2  # after skill add, after file writes
    assert skill.name == "story-design"
    skills_added = [o for o in session.added if isinstance(o, Skill)]
    files_added = [o for o in session.added if isinstance(o, SkillFile)]
    assert [s.name for s in skills_added] == ["story-design"]
    assert skills_added[0].model == "high"
    assert {f.path: f.content for f in files_added} == {
        "SKILL.md": "md", "references/a.md": "a",
    }
    assert session.deleted == []


async def test_upsert_updates_existing_and_deletes_stale_files():
    existing = _existing_skill()
    stale = _file_row("references/old.md", "stale")
    kept = _file_row("SKILL.md", "v1")
    session = _RecordingSession(
        _FakeResult(one=existing),
        _FakeResult(rows=[kept, stale]),
    )
    skill = await upsert_skill(
        session,
        name="story-design",
        description="new desc",
        model="low",
        compatibility="c",
        files={"SKILL.md": "v2", "references/new.md": "n"},
    )

    assert session.commits == 0
    assert skill is existing
    assert (existing.description, existing.model, existing.compatibility) == (
        "new desc", "low", "c",
    )
    assert kept.content == "v2"                 # updated in place
    added_paths = {f.path for f in session.added if isinstance(f, SkillFile)}
    assert added_paths == {"references/new.md"}  # added, not re-created
    assert session.deleted == [stale]            # absent from bundle → deleted
    assert session.flushes == 1                  # no skill add → one flush


async def test_upsert_skill_select_filters_platform_scope():
    """Project-scoped rows with the same name must never be matched."""
    session = _RecordingSession(
        _FakeResult(one=None),
        _FakeResult(rows=[]),
    )
    await upsert_skill(
        session, name="x", description="", model="medium",
        compatibility=None, files={"SKILL.md": ""},
    )
    skill_select = str(session.statements[0])
    assert "project_id IS NULL" in skill_select


# ── seed-level regression: the tier fix ──────────────────────────────────────


class _FakeSessionCtx:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_seed_maps_opus_skill_to_high(monkeypatch, tmp_path):
    (tmp_path / "my-skill").mkdir()
    (tmp_path / "my-skill" / "SKILL.md").write_text(
        "---\nname: my-skill\nmodel: opus\n---\n# body\n"
    )
    session = _RecordingSession(_FakeResult(one=None), _FakeResult(rows=[]))
    monkeypatch.setattr(
        "bheembhai.database._sessionmaker", lambda: _FakeSessionCtx(session)
    )

    await seed_default_skills(tmp_path)

    skills_added = [o for o in session.added if isinstance(o, Skill)]
    assert len(skills_added) == 1
    assert skills_added[0].model == "high"  # was "medium" before the fix
    assert session.commits == 1             # seed keeps per-skill commit


def test_model_tier_map_covers_skill_md_convention():
    assert MODEL_TIER_MAP == {
        "opus": "high", "sonnet": "medium", "haiku": "low",
        "high": "high", "medium": "medium", "low": "low",
    }
