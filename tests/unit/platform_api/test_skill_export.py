"""Unit — admin skill zip export: deterministic zip building that round-trips
through the import analyzer. Pure logic — no DB, no framework.
"""

import io
import zipfile

from platform_api.skill_export import build_skills_zip
from platform_api.skill_import import analyze_zip

# ── helpers ──────────────────────────────────────────────────────────────────


def _md(name: str, body: str = "") -> str:
    return f"---\nname: {name}\n---\n{body}\n"


def _files(name: str, **extra: str) -> dict[str, str]:
    files = {"SKILL.md": _md(name)}
    for path, content in extra.items():
        files[path] = content
    return files


def _namelist(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


# ── layout + ordering ────────────────────────────────────────────────────────


def test_layout_puts_each_file_under_skills_name():
    data = build_skills_zip(
        [("alpha", _files("alpha", **{"references/a.md": "a"}))])
    assert _namelist(data) == [
        "skills/alpha/SKILL.md", "skills/alpha/references/a.md"]


def test_skill_md_first_then_rest_sorted():
    files = _files("s")
    files.update({"zebra.md": "z", "alpha/b.md": "b", "notes/empty.txt": ""})
    data = build_skills_zip([("s", files)])
    assert _namelist(data) == [
        "skills/s/SKILL.md",
        "skills/s/alpha/b.md",
        "skills/s/notes/empty.txt",
        "skills/s/zebra.md",
    ]


def test_skills_follow_input_order():
    data = build_skills_zip([
        ("second", _files("second")),
        ("first", _files("first")),
        ("third", _files("third")),
    ])
    assert _namelist(data) == [
        "skills/second/SKILL.md",
        "skills/first/SKILL.md",
        "skills/third/SKILL.md",
    ]


def test_unicode_names_and_paths():
    files = _files("campañas")
    files["references/guía.md"] = "guía"
    data = build_skills_zip([("campañas", files)])
    assert _namelist(data) == [
        "skills/campañas/SKILL.md",
        "skills/campañas/references/guía.md",
    ]


def test_skill_with_only_skill_md():
    data = build_skills_zip([("solo", _files("solo"))])
    assert _namelist(data) == ["skills/solo/SKILL.md"]


def test_timestamps_are_pinned_for_byte_determinism():
    data = build_skills_zip([("a", _files("a"))])
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert all(
            info.date_time == (1980, 1, 1, 0, 0, 0) for info in zf.infolist())


def test_build_is_byte_deterministic():
    files = _files("a", **{"references/x.md": "x"})
    assert build_skills_zip([("a", files)]) == build_skills_zip([("a", files)])


# ── round-trips through the import contract ──────────────────────────────────


def test_empty_files_round_trip():
    files = _files("a")
    files["notes/empty.txt"] = ""
    bundle = analyze_zip(build_skills_zip([("a", files)])).skills[0]
    assert bundle.files == files


def test_export_round_trips_through_analyze_zip():
    """The export contract: what goes out analyzes back to the same rows —
    including external refs stored inside the skill (dependency-complete),
    so the zip is deployable to another instance."""
    alpha = _files("alpha", **{"references/a.md": "ref a"})
    beta = _files("beta", **{
        "tools/REGISTRY.md": "# registry",
        "examples/e.md": "example",
    })
    # A reference brought inside the skill at import time stays resolvable
    # after export: the path lives in the bundle and the link is in-skill.
    beta["SKILL.md"] = _md("beta", "[r](tools/REGISTRY.md)")

    analysis = analyze_zip(build_skills_zip([("alpha", alpha), ("beta", beta)]))
    by_name = {b.name: b for b in analysis.skills}
    assert analysis.invalid_dirs == []
    assert analysis.other_entries == []
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"].files == alpha
    assert by_name["beta"].files == beta
    assert by_name["beta"].missing_referenced == []
