"""Unit — admin skill zip import: structure validation, zip-slip + zip-bomb
guards, frontmatter/tier mapping through the bundle path, and the file-
reference scan. Pure logic — no DB, no framework.
"""

import io
import zipfile

import pytest

from platform_api import skill_import
from platform_api.skill_import import (
    SkillBundle,
    ZipValidationError,
    analyze_zip,
    bundle_files_with_external,
    scan_referenced_files,
)

# ── builders ─────────────────────────────────────────────────────────────────


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _md(frontmatter: str, body: str = "") -> bytes:
    return f"---\n{frontmatter}\n---\n{body}\n".encode()


def _skill_files(name: str = "my-skill", **extra: bytes) -> dict[str, bytes]:
    entries = {f"skills/{name}/SKILL.md": _md(f"name: {name}")}
    for path, content in extra.items():
        entries[f"skills/{name}/{path}"] = content
    return entries


# ── happy paths ──────────────────────────────────────────────────────────────


def test_analyze_single_skill_with_supporting_files():
    analysis = analyze_zip(_zip(_skill_files(
        **{
            "references/a.md": b"ref a",
            "templates/t.md": b"tmpl",
            "scripts/deep/x.sh": b"#!/bin/sh\n",
        },
    )))

    assert len(analysis.skills) == 1
    bundle = analysis.skills[0]
    assert bundle.name == "my-skill"
    assert bundle.directory == "skills/my-skill"
    assert bundle.model == "medium"  # no model in frontmatter
    assert bundle.description == ""
    assert bundle.exists is False
    assert bundle.warnings == []
    assert list(bundle.files) == [
        "SKILL.md", "references/a.md", "scripts/deep/x.sh", "templates/t.md",
    ]  # SKILL.md first, rest sorted
    assert analysis.invalid_dirs == []
    assert analysis.other_entries == []


def test_analyze_preserves_zip_order_for_multiple_skills():
    entries = {
        "skills/zzz/SKILL.md": _md("name: zzz"),
        "skills/aaa/SKILL.md": _md("name: aaa"),
    }
    analysis = analyze_zip(_zip(entries))
    assert [b.name for b in analysis.skills] == ["zzz", "aaa"]


def test_analyze_ignores_root_entries_outside_skills():
    analysis = analyze_zip(_zip({
        "tools/helper.py": b"def h(): pass\n", "loose.md": b"loose",
        "skills/a/SKILL.md": _md("name: a"),
    }))
    assert [b.name for b in analysis.skills] == ["a"]
    assert analysis.other_entries == ["tools/helper.py", "loose.md"]


def test_analyze_reports_dir_without_skill_md_as_invalid():
    analysis = analyze_zip(_zip({
        "skills/no-md/readme.txt": b"no SKILL.md here",
        "skills/ok/SKILL.md": _md("name: ok"),
    }))
    assert [b.name for b in analysis.skills] == ["ok"]
    assert analysis.invalid_dirs == ["skills/no-md"]


def test_analyze_maps_opus_model_to_high():
    entries = {"skills/a/SKILL.md": _md("name: a\nmodel: opus")}
    bundle = analyze_zip(_zip(entries)).skills[0]
    assert bundle.model == "high"


def test_analyze_backslash_paths_are_normalized():
    analysis = analyze_zip(_zip({"skills\\a\\SKILL.md": _md("name: a")}))
    assert [b.name for b in analysis.skills] == ["a"]


def test_analyze_unicode_dirname_and_empty_skill_md():
    analysis = analyze_zip(_zip({
        "skills/héllo/SKILL.md": _md("name: héllo\nmodel: haiku"),
        "skills/empty/SKILL.md": b"",
    }))
    by_name = {b.name: b for b in analysis.skills}
    assert by_name["héllo"].model == "low"
    assert by_name["empty"].files == {"SKILL.md": ""}  # empty files allowed
    assert by_name["empty"].description == ""


def test_analyze_unparseable_frontmatter_defaults_with_warning():
    entries = {"skills/a/SKILL.md": b"---\nname: [unclosed\n---\n"}
    bundle = analyze_zip(_zip(entries)).skills[0]
    assert bundle.name == "a"
    assert bundle.model == "medium"
    assert "unparseable" in " ".join(bundle.warnings)


# ── fatal cases ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("data,message", [
    (b"", "Not a valid zip file"),
    (_zip({}), "The zip is empty"),
    (_zip({"other/file.md": b"x"}), "no skills/ folder"),
    (_zip({"skills/": b"", "skills/a/": b""}), "no skills/ folder"),
])
def test_analyze_fatal_structure_problems(data, message):
    with pytest.raises(ZipValidationError, match=message):
        analyze_zip(data)


@pytest.mark.parametrize("entry", [
    "../evil.md",
    "skills/a/../../evil.md",
    "skills/../../evil.md",
    "/abs/file.md",
    "C:/win/file.md",
])
def test_analyze_fatal_zip_slip(entry):
    entries = {"skills/a/SKILL.md": _md("name: a"), entry: b"x"}
    with pytest.raises(ZipValidationError):
        analyze_zip(_zip(entries))


def test_analyze_fatal_duplicate_entry_names():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("skills/a/SKILL.md", _md("name: a"))
        with pytest.warns(UserWarning):  # zipfile flags the dup we're creating
            zf.writestr("skills/a/SKILL.md", _md("name: a"))
    with pytest.raises(ZipValidationError, match="duplicate entry"):
        analyze_zip(buf.getvalue())


def test_analyze_fatal_duplicate_resolved_skill_names():
    entries = {
        "skills/dir-a/SKILL.md": _md("name: same"),
        "skills/dir-b/SKILL.md": _md("name: same"),
    }
    with pytest.raises(ZipValidationError, match="duplicate skill name 'same'"):
        analyze_zip(_zip(entries))


def test_analyze_fatal_entry_count_cap(monkeypatch):
    monkeypatch.setattr(skill_import, "MAX_ENTRY_COUNT", 2)
    entries = {
        "skills/a/SKILL.md": _md("name: a"),
        "skills/a/r1.md": b"1",
        "skills/a/r2.md": b"2",
    }
    with pytest.raises(ZipValidationError, match="too many entries"):
        analyze_zip(_zip(entries))


def test_analyze_fatal_single_file_cap(monkeypatch):
    monkeypatch.setattr(skill_import, "MAX_SINGLE_FILE_BYTES", 100)
    entries = {"skills/a/SKILL.md": _md("name: a") + b"x" * 150}
    with pytest.raises(ZipValidationError, match="file too large"):
        analyze_zip(_zip(entries))


def test_analyze_budgets_measure_decompressed_content():
    """6 × 9 MiB of zeros: ~55 KB compressed (well under the 5 MB upload cap)
    but 54 MiB decompressed (over the 50 MiB budget) — proving the zip-bomb
    guard measures post-decompression, not the wire size."""
    entries = {"skills/a/SKILL.md": _md("name: a")}
    for i in range(6):
        entries[f"skills/a/data{i}.bin"] = b"\x00" * (9 * 1024 * 1024)
    compressed = _zip(entries)
    assert len(compressed) < 5 * 1024 * 1024
    with pytest.raises(ZipValidationError, match="decompresses beyond"):
        analyze_zip(compressed)


# ── scan_referenced_files ────────────────────────────────────────────────────


def test_scan_flags_missing_link_target():
    refs = scan_referenced_files("[see](references/ctx.md)", {"SKILL.md"},
                                 "skills/test")
    assert refs.missing == ["references/ctx.md"]
    assert refs.external == []


def test_scan_passes_present_link_target():
    refs = scan_referenced_files("[see](references/ctx.md)",
                                 {"references/ctx.md"}, "skills/test")
    assert refs.missing == []


def test_scan_normalizes_dot_prefix_and_anchor():
    refs = scan_referenced_files("[x](./templates/t.md#sec)", set(),
                                 "skills/test")
    assert refs.missing == ["templates/t.md"]


def test_scan_finds_bare_path_tokens_without_links():
    skill_md = "Load references/context.md and examples/e.md first."
    refs = scan_referenced_files(skill_md, {"examples/e.md"}, "skills/test")
    assert refs.missing == ["references/context.md"]


def test_scan_flags_repo_level_links_absent_from_the_zip():
    """Links pointing outside the skill dir with no matching zip entry are
    genuinely missing — flagged with the reason."""
    skill_md = (
        "| Platform | Guide |\n"
        "|----------|-------|\n"
        "| **Google Ads** | [google-ads.md](../../tools/integrations/google-ads.md) |\n"
        "[tools registry](../../tools/REGISTRY.md)"
    )
    refs = scan_referenced_files(skill_md, set(), "skills/campaigns")
    assert refs.missing == [
        "tools/REGISTRY.md (not importable — outside the skill directory)",
        "tools/integrations/google-ads.md (not importable — outside the skill directory)",
    ]
    assert refs.external == []


def test_scan_treats_in_zip_out_of_dir_refs_as_external_not_missing():
    """A zip that carries tools/ alongside skills/: refs to it are satisfied
    by the zip — reported as external, not as missing."""
    skill_md = "[r](../../tools/REGISTRY.md)"
    refs = scan_referenced_files(
        skill_md, set(), "skills/campaigns",
        zip_entries={"tools/REGISTRY.md"},
    )
    assert refs.missing == []
    assert refs.external == ["tools/REGISTRY.md"]


def test_scan_splits_external_and_missing_refs():
    skill_md = "[a](../../tools/REGISTRY.md) [b](../../tools/gone.md)"
    refs = scan_referenced_files(
        skill_md, set(), "skills/campaigns",
        zip_entries={"tools/REGISTRY.md"},
    )
    assert refs.external == ["tools/REGISTRY.md"]
    assert refs.missing == [
        "tools/gone.md (not importable — outside the skill directory)",
    ]


def test_scan_flags_sibling_skill_and_bare_dotdot_refs():
    skill_md = "[s](../other/SKILL.md) see ../../references/x.md"
    refs = scan_referenced_files(skill_md, set(), "skills/campaigns")
    assert refs.missing == [
        "references/x.md (not importable — outside the skill directory)",
        "skills/other/SKILL.md (not importable — outside the skill directory)",
    ]


def test_scan_collapses_mid_path_dotdot():
    refs = scan_referenced_files("[x](references/../x.md)", set(),
                                 "skills/test")
    assert refs.missing == ["x.md"]


def test_scan_ignores_urls_anchors_absolute_and_repo_escaping_refs():
    skill_md = (
        "[site](https://example.com/x.md) [top](#top) "
        "[abs](/etc/passwd) [out](../../../outside.md)"
    )
    refs = scan_referenced_files(skill_md, set(), "skills/campaigns")
    assert refs.missing == [] and refs.external == []


def test_scan_returns_sorted_deduplicated_missing():
    skill_md = "[b](references/b.md) [a](references/a.md) [b2](references/b.md)"
    refs = scan_referenced_files(skill_md, set(), "skills/test")
    assert refs.missing == ["references/a.md", "references/b.md"]


# ── external files brought in with the skill ─────────────────────────────────


def _bundle(files, external_files, external_refs=None):
    if external_refs is None:
        # the common doc form for a repo-root reference from skills/<name>/
        external_refs = {f"../../{path}": path for path in external_files}
    return SkillBundle(
        directory="skills/campaigns", name="campaigns", description="",
        model="medium", compatibility=None, files=files,
        external_references=list(external_files),
        external_refs=external_refs,
        external_files=external_files,
    )


def test_analyze_populates_external_files_contents():
    """Zip-backed out-of-dir refs: contents carried on the bundle for import."""
    analysis = analyze_zip(_zip({
        "skills/campaigns/SKILL.md": _md(
            "name: campaigns", "[r](../../tools/REGISTRY.md)"),
        "tools/REGISTRY.md": b"# registry",
    }))
    bundle = analysis.skills[0]
    assert bundle.external_references == ["tools/REGISTRY.md"]
    assert bundle.external_refs == {
        "../../tools/REGISTRY.md": "tools/REGISTRY.md"}
    assert bundle.external_files == {"tools/REGISTRY.md": "# registry"}
    assert "tools" not in bundle.files  # skill's own files untouched
    assert "tools/REGISTRY.md" not in bundle.files


def test_bundle_files_with_external_merges_and_rewrites():
    skill_md = "[r](../../tools/REGISTRY.md) see ../../tools/clis/x.js"
    bundle = _bundle(
        {"SKILL.md": skill_md, "references/a.md": "a"},
        {"tools/REGISTRY.md": "# registry", "tools/clis/x.js": "// x"},
    )
    files = bundle_files_with_external(bundle)
    assert files == {
        "SKILL.md": "[r](tools/REGISTRY.md) see tools/clis/x.js",
        "references/a.md": "a",
        "tools/REGISTRY.md": "# registry",
        "tools/clis/x.js": "// x",
    }
    assert bundle.files["SKILL.md"] == skill_md  # bundle not mutated


def test_bundle_files_with_external_rewrites_single_dotdot_refs():
    """A `../`-style ref (sibling path) is rewritten via its exact raw form."""
    bundle = _bundle(
        {"SKILL.md": "[s](../shared-notes.md)"},
        {"skills/shared-notes.md": "notes"},
        external_refs={"../shared-notes.md": "skills/shared-notes.md"},
    )
    files = bundle_files_with_external(bundle)
    assert files["SKILL.md"] == "[s](skills/shared-notes.md)"
    assert files["skills/shared-notes.md"] == "notes"


def test_bundle_files_with_external_keeps_own_file_on_path_collision():
    bundle = _bundle(
        {"SKILL.md": "[r](../../tools/REGISTRY.md)",
         "tools/REGISTRY.md": "skill-local"},
        {"tools/REGISTRY.md": "repo-level"},
    )
    files = bundle_files_with_external(bundle)
    assert files["tools/REGISTRY.md"] == "skill-local"  # own file wins
    assert files["SKILL.md"] == "[r](tools/REGISTRY.md)"  # link still rewritten


def test_bundle_files_with_external_preserves_anchor_on_rewrite():
    """The raw form matches as a substring, so a trailing `#anchor` survives."""
    bundle = _bundle(
        {"SKILL.md": "[r](../../tools/REGISTRY.md#registry)"},
        {"tools/REGISTRY.md": "# registry"},
    )
    files = bundle_files_with_external(bundle)
    assert files["SKILL.md"] == "[r](tools/REGISTRY.md#registry)"


def test_bundle_files_with_external_noop_without_externals():
    bundle = _bundle({"SKILL.md": "# plain"}, {})
    assert bundle_files_with_external(bundle) is bundle.files
