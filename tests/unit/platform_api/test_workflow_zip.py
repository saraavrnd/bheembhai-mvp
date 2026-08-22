"""Unit — workflow zip export/import: build determinism + layout, the
analyze table (workflows/skills/policies, exists flags), slug matching,
zip-slip/zip-bomb guard reuse, and build→analyze round-trip. Pure logic —
no DB, no framework.
"""

import io
import zipfile

import pytest
import yaml

from platform_api.skill_import import ZipValidationError
from platform_api.workflow_zip import (
    PolicyExport,
    WorkflowExport,
    analyze_zip,
    build_workflows_zip,
    slugify,
)

# ── builders ─────────────────────────────────────────────────────────────────


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _unzip(data: bytes) -> dict[str, str]:
    out = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            out[info.filename] = zf.read(info).decode()
    return out


def _wf_yaml(name="wf", skills=("story-design",)) -> str:
    steps = "\n".join(
        f"  - id: step{i}\n    skill: {s}\n" for i, s in enumerate(skills)
    )
    return f"workflow: {name}\nversion: 1\nstart: step0\nsteps:\n{steps}"


def _skill_md(name: str) -> bytes:
    return f"---\nname: {name}\n---\nbody\n".encode()


def _wf_wrapper(name: str, version=1, content: str | None = None, **extra) -> bytes:
    doc = {"name": name, "version": version, "description": "", "is_active": True}
    doc.update(extra)
    doc["content"] = content if content is not None else _wf_yaml(name)
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _policy_wrapper(name: str, version=1, content: str | None = None, **extra) -> bytes:
    doc = {"name": name, "version": version, "description": "", "is_active": True}
    doc.update(extra)
    doc["content"] = content or f"policy: {name}\nversion: 1\ngates: {{}}\n"
    return yaml.safe_dump(doc, sort_keys=False).encode()


def _wf_export(name="Story Delivery", version=3, **kw) -> WorkflowExport:
    return WorkflowExport(
        name=name,
        version=version,
        yaml_content=_wf_yaml(name, ("story-design", "code-review")),
        **kw,
    )


# ── build ────────────────────────────────────────────────────────────────────


def test_build_is_byte_deterministic():
    wf = _wf_export(
        category="Delivery",
        is_active=False,
        policies=[
            PolicyExport(name="Strict Gate", version=1, yaml_content="policy: sg\n"),
            PolicyExport(name="Fast", version=2, yaml_content="policy: f\n"),
        ],
        skills={
            "story-design": {"SKILL.md": "# sd\n", "references/x.md": "x"},
            "code-review": {"SKILL.md": "# cr\n"},
        },
    )
    assert build_workflows_zip([wf]) == build_workflows_zip([wf])


def test_build_entry_order_and_layout():
    wf = _wf_export(
        category="Delivery",
        is_active=False,
        policies=[
            PolicyExport(name="Strict Gate", version=1, yaml_content="policy: sg\n"),
            PolicyExport(name="Fast", version=2, yaml_content="policy: f\n"),
        ],
        skills={
            "story-design": {"SKILL.md": "# sd\n", "references/x.md": "x"},
            "code-review": {"SKILL.md": "# cr\n"},
        },
    )
    tree = _unzip(build_workflows_zip([wf]))
    # workflows first, then policies per workflow (sorted by name+version),
    # then the merged skills section (name order, SKILL.md first per skill)
    assert list(tree) == [
        "workflows/story-delivery.yaml",
        "policies/story-delivery/fast.yaml",
        "policies/story-delivery/strict-gate.yaml",
        "skills/code-review/SKILL.md",
        "skills/story-design/SKILL.md",
        "skills/story-design/references/x.md",
    ]

    manifest = yaml.safe_load(tree["workflows/story-delivery.yaml"])
    assert manifest["name"] == "Story Delivery"
    assert manifest["version"] == 3
    assert manifest["category"] == "Delivery"
    assert manifest["is_active"] is False
    assert manifest["content"] == _wf_yaml("Story Delivery",
                                            ("story-design", "code-review"))

    fast = yaml.safe_load(tree["policies/story-delivery/fast.yaml"])
    assert (fast["name"], fast["version"]) == ("Fast", 2)
    assert fast["content"] == "policy: f\n"


def test_build_skills_appear_once_when_referenced_by_two_workflows():
    wf1 = _wf_export(name="One", skills={
        "story-design": {"SKILL.md": "# sd\n"},
        "code-review": {"SKILL.md": "# cr\n"},
    })
    wf2 = _wf_export(name="Two", skills={
        "story-design": {"SKILL.md": "# sd\n"},  # same content — deduped
    })
    tree = _unzip(build_workflows_zip([wf1, wf2]))
    assert list(tree).count("skills/story-design/SKILL.md") == 1
    # skill order = first reference: wf1's name-sorted skills, then wf2's new ones
    skill_names = [k for k in tree if k.startswith("skills/")]
    assert skill_names == [
        "skills/code-review/SKILL.md",
        "skills/story-design/SKILL.md",
    ]


def test_build_raises_on_workflow_slug_collision():
    with pytest.raises(ValueError, match="slug collision"):
        build_workflows_zip([
            _wf_export(name="my wf"),
            _wf_export(name="my-wf"),
        ])


def test_build_raises_on_policy_slug_collision():
    wf = _wf_export(policies=[
        PolicyExport(name="strict gate", version=1, yaml_content="policy: a\n"),
        PolicyExport(name="strict-gate", version=2, yaml_content="policy: b\n"),
    ])
    with pytest.raises(ValueError, match="policy slug collision"):
        build_workflows_zip([wf])


def test_slugify():
    assert slugify("Story Delivery") == "story-delivery"
    assert slugify("A.B_C 9") == "a-b-c-9"
    assert slugify("###") == "untitled"


# ── analyze: happy path / round-trip ─────────────────────────────────────────


def test_round_trip_build_then_analyze():
    wf = _wf_export(
        category="Delivery",
        is_active=False,
        policies=[
            PolicyExport(name="Strict Gate", version=1, yaml_content="policy: sg\n"),
        ],
        skills={"story-design": {"SKILL.md": "# sd\n"}, "code-review": {"SKILL.md": "# cr\n"}},
    )
    analysis = analyze_zip(build_workflows_zip([wf]))

    assert len(analysis.workflows) == 1
    entry = analysis.workflows[0]
    assert (entry.name, entry.version) == ("Story Delivery", 3)
    assert entry.slug == "story-delivery"
    assert entry.category == "Delivery"
    assert entry.is_active is False
    assert entry.yaml_content == _wf_yaml("Story Delivery",
                                          ("story-design", "code-review"))
    assert entry.referenced_skills == ["story-design", "code-review"]
    assert entry.warnings == []
    assert entry.exists is False

    assert [b.name for b in analysis.skills] == ["code-review", "story-design"]
    assert [b.files.get("SKILL.md") for b in analysis.skills] == ["# cr\n", "# sd\n"]

    assert len(entry.policies) == 1
    pol = entry.policies[0]
    assert (pol.name, pol.version, pol.workflow_slug, pol.slug) == (
        "Strict Gate", 1, "story-delivery", "strict-gate",
    )
    assert pol.yaml_content == "policy: sg\n"
    assert pol.exists is False

    assert analysis.invalid_workflows == []
    assert analysis.invalid_skills == []
    assert analysis.orphan_policies == []
    assert analysis.missing_skills == []
    assert analysis.other_entries == []
    assert analysis.warnings == []


def test_analyze_tolerates_content_without_trailing_newline():
    content = "workflow: w\nversion: 1\nsteps: []\n"[:-1]  # no trailing \n
    data = _zip({"workflows/w.yaml": _wf_wrapper("w", content=content)})
    entry = analyze_zip(data).workflows[0]
    assert entry.yaml_content == content
    assert entry.referenced_skills == []


def test_analyze_accepts_yml_extension():
    data = _zip({"workflows/w.yml": _wf_wrapper("w")})
    assert analyze_zip(data).workflows[0].name == "w"


def test_analyze_skill_name_falls_back_to_step_id():
    content = (
        "workflow: w\nversion: 1\nstart: design\n"
        "steps:\n  - id: design\n    model: high\n"
    )
    data = _zip({"workflows/w.yaml": _wf_wrapper("w", content=content)})
    entry = analyze_zip(data).workflows[0]
    assert entry.referenced_skills == ["design"]


def test_analyze_matches_policies_by_slugified_name_not_filename():
    # workflow file stem ("my-workflow") differs from slugify(name)
    # ("other-name") — policies addressed by EITHER spelling must attach.
    entries = {
        "workflows/my-workflow.yaml": _wf_wrapper("Other Name"),
        "policies/my-workflow/p1.yaml": _policy_wrapper("P One"),
        "policies/other-name/p2.yaml": _policy_wrapper("P Two"),
    }
    analysis = analyze_zip(_zip(entries))
    assert len(analysis.orphan_policies) == 0
    assert [p.name for p in analysis.workflows[0].policies] == ["P One", "P Two"]


# ── analyze: fatal guards (shared with skill_import) ─────────────────────────


def test_analyze_rejects_garbage():
    with pytest.raises(ZipValidationError, match="Not a valid zip file"):
        analyze_zip(b"not a zip")


def test_analyze_rejects_empty_zip():
    with pytest.raises(ZipValidationError, match="The zip is empty"):
        analyze_zip(_zip({}))


def test_analyze_rejects_zip_without_workflows_section():
    data = _zip({"skills/x/SKILL.md": _skill_md("x")})
    with pytest.raises(ZipValidationError, match="no workflows/ folder"):
        analyze_zip(data)


def test_analyze_rejects_zip_slip_entries():
    data = _zip({"workflows/../../evil.yaml": b"name: x\ncontent: y\n"})
    with pytest.raises(ZipValidationError, match="escapes"):
        analyze_zip(data)


def test_analyze_rejects_duplicate_workflow_name_version():
    entries = {
        "workflows/a.yaml": _wf_wrapper("dup", version=1),
        "workflows/b.yaml": _wf_wrapper("dup", version=1),
    }
    with pytest.raises(ZipValidationError, match="duplicate workflow 'dup' v1"):
        analyze_zip(_zip(entries))


def test_analyze_allows_same_name_different_versions():
    entries = {
        "workflows/a.yaml": _wf_wrapper("dup", version=1),
        "workflows/b.yaml": _wf_wrapper("dup", version=2),
    }
    analysis = analyze_zip(_zip(entries))
    assert [w.version for w in analysis.workflows] == [1, 2]


def test_analyze_rejects_duplicate_policy_in_one_workflow():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w"),
        "policies/w/p1.yaml": _policy_wrapper("pol", version=1),
        "policies/w/p2.yaml": _policy_wrapper("pol", version=1),
    }
    with pytest.raises(ZipValidationError, match="duplicate policy 'pol' v1"):
        analyze_zip(_zip(entries))


def test_analyze_rejects_duplicate_skill_names_in_zip():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w", content=_wf_yaml("w", ("s",))),
        "skills/x/SKILL.md": _skill_md("s"),
        "skills/y/SKILL.md": _skill_md("s"),
    }
    with pytest.raises(ZipValidationError, match="duplicate skill name 's'"):
        analyze_zip(_zip(entries))


# ── analyze: non-fatal rows ──────────────────────────────────────────────────


def test_analyze_reports_invalid_workflow_wrapper():
    entries = {
        "workflows/good.yaml": _wf_wrapper("good"),
        "workflows/bad.yaml": b"not: [yaml: at: all\n",
    }
    analysis = analyze_zip(_zip(entries))
    assert [w.name for w in analysis.workflows] == ["good"]
    assert analysis.invalid_workflows == ["workflows/bad.yaml"]


def test_analyze_reports_orphan_policies():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w"),
        "policies/ghost/p.yaml": _policy_wrapper("G"),
    }
    analysis = analyze_zip(_zip(entries))
    assert analysis.orphan_policies == ["policies/ghost/p.yaml"]
    assert analysis.workflows[0].policies == []


def test_analyze_reports_broken_policy_wrapper_as_orphan():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w"),
        "policies/w/p.yaml": b"name: [nested\n",
    }
    analysis = analyze_zip(_zip(entries))
    assert analysis.orphan_policies == ["policies/w/p.yaml"]


def test_analyze_warns_on_unparseable_workflow_content():
    entries = {"workflows/w.yaml": _wf_wrapper("w", content="not: [yaml\n")}
    analysis = analyze_zip(_zip(entries))
    entry = analysis.workflows[0]
    assert entry.referenced_skills == []
    assert entry.warnings == [
        "workflow YAML unparseable — referenced skills unknown",
    ]


def test_analyze_warns_on_unparseable_policy_content():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w"),
        "policies/w/p.yaml": _policy_wrapper("P", content="not: [yaml\n"),
    }
    analysis = analyze_zip(_zip(entries))
    assert analysis.workflows[0].policies[0].warnings == ["policy YAML unparseable"]


def test_analyze_reports_missing_referenced_skills():
    entries = {"workflows/w.yaml": _wf_wrapper("w", content=_wf_yaml("w", ("story-design", "code-review")))}
    analysis = analyze_zip(_zip(entries))
    assert analysis.missing_skills == ["code-review", "story-design"]
    assert "The zip carries no skills/ section" in analysis.warnings
    assert "skill 'story-design' is not in the zip" in analysis.workflows[0].warnings


def test_analyze_reports_invalid_skill_dirs_and_other_entries():
    entries = {
        "workflows/w.yaml": _wf_wrapper("w"),
        "skills/broken/references/x.md": b"ref",  # no SKILL.md
        "skills/ok/SKILL.md": _skill_md("ok"),
        "README.md": b"hi",
        "workflows/sub/extra.yaml": b"name: x\ncontent: y\n",
    }
    analysis = analyze_zip(_zip(entries))
    assert analysis.invalid_skills == ["skills/broken"]
    assert [b.name for b in analysis.skills] == ["ok"]
    assert analysis.other_entries == ["README.md", "workflows/sub/extra.yaml"]
