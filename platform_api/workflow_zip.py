"""Workflow zip export/import — pure zip building and parsing (no FastAPI, no DB).

Mirrors the skill zip pair (``skill_export`` / ``skill_import``) for workflows:

* ``build_workflows_zip`` — deterministic zip:
  ``workflows/<slug>.yaml`` manifests (one per workflow, wrapping
  name/version/category/content), ``policies/<workflow-slug>/<policy-slug>.yaml``
  for each policy of an exported workflow, and a ``skills/<name>/…`` section
  for every referenced skill (delegated to ``skill_export.build_skills_zip``
  so the skill format never drifts).
* ``analyze_zip`` — parses + validates an uploaded zip into a
  ``WorkflowZipAnalysis`` (workflow/skill/policy rows with warnings), reusing
  the skill importer's zip-slip and zip-bomb guards
  (``skill_import.collect_entries`` / ``build_skill_bundles``).

The wrapper files are ``{name, version, description, category, is_active,
content}`` YAML docs — the slug is a filename-only key, the wrapper's ``name``
is authoritative, and the importer never derives a name back from a slug.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from platform_api.skill_export import build_skills_zip
from platform_api.skill_import import (
    SKILL_DIR,
    ZipValidationError,
    build_skill_bundles,
    collect_entries,
)

if TYPE_CHECKING:
    from platform_api.skill_import import SkillBundle

WORKFLOWS_DIR = "workflows"
POLICIES_DIR = "policies"

# Pin all ZipInfo timestamps so exports are byte-deterministic — the import
# path ignores entry timestamps, so the round-trip is unaffected (same
# contract as skill_export).
_EPOCH = (1980, 1, 1, 0, 0, 0)

_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase with non-alphanumerics collapsed to '-' — filename key only.

    Lossy by design (see module docstring): the wrapper's ``name`` field is
    authoritative on import.
    """
    return _SLUG_BAD.sub("-", name.lower()).strip("-") or "untitled"


# ── Export model ─────────────────────────────────────────────────────────────


@dataclass
class PolicyExport:
    name: str
    version: int
    yaml_content: str
    description: str = ""
    is_active: bool = True


@dataclass
class WorkflowExport:
    """One workflow + its policies + the skills it references.

    ``skills`` maps referenced skill name → ``{path: content}`` (the same
    shape ``build_skills_zip`` consumes). A skill referenced by several
    workflows appears once in the zip — the first occurrence's content wins
    (per scope, name → row is 1:1, so variants cannot actually differ).
    """

    name: str
    version: int
    yaml_content: str
    description: str = ""
    category: str | None = None
    is_active: bool = True
    policies: list[PolicyExport] = field(default_factory=list)
    skills: dict[str, dict[str, str]] = field(default_factory=dict)


# ── Analysis model ───────────────────────────────────────────────────────────


@dataclass
class PolicyEntry:
    slug: str
    workflow_slug: str
    name: str
    version: int
    description: str
    is_active: bool
    yaml_content: str
    warnings: list[str] = field(default_factory=list)
    exists: bool = False  # populated by the endpoint (DB query)


@dataclass
class WorkflowEntry:
    slug: str
    name: str
    version: int
    description: str
    category: str | None
    is_active: bool
    yaml_content: str
    warnings: list[str] = field(default_factory=list)
    referenced_skills: list[str] = field(default_factory=list)
    policies: list[PolicyEntry] = field(default_factory=list)
    exists: bool = False  # populated by the endpoint (DB query)


@dataclass
class WorkflowZipAnalysis:
    """Analysis table data: workflow/skill/policy rows plus non-importables."""

    workflows: list[WorkflowEntry] = field(default_factory=list)  # zip order
    skills: list[SkillBundle] = field(default_factory=list)  # zip order
    invalid_workflows: list[str] = field(default_factory=list)
    invalid_skills: list[str] = field(default_factory=list)  # skills/<name> w/o SKILL.md
    orphan_policies: list[str] = field(default_factory=list)  # no matching workflow
    missing_skills: list[str] = field(default_factory=list)  # referenced, not in zip
    other_entries: list[str] = field(default_factory=list)  # outside all sections
    warnings: list[str] = field(default_factory=list)


# ── Export builder ───────────────────────────────────────────────────────────


def _dump_wrapper(
    content: str,
    *,
    name: str,
    version: int,
    description: str = "",
    category: str | None = None,
    is_active: bool = True,
) -> str:
    """YAML wrapper doc: fixed key order, ``content`` last as a block scalar.

    ``safe_dump`` with ``sort_keys=False`` preserves insertion order, so
    identical input yields identical bytes (deterministic export).
    """
    doc: dict = {
        "name": name,
        "version": version,
        "description": description,
        "is_active": is_active,
    }
    if category is not None:
        doc["category"] = category
    doc["content"] = content
    return yaml.safe_dump(
        doc, sort_keys=False, default_flow_style=False, allow_unicode=True
    )


def build_workflows_zip(workflows: Iterable[WorkflowExport]) -> bytes:
    """Build an import-compatible workflows zip from :class:`WorkflowExport`s.

    Entry layout: all ``workflows/<slug>.yaml`` manifests first (input order),
    then ``policies/<workflow-slug>/<policy-slug>.yaml`` grouped by workflow
    (policies sorted by name+version), then the merged ``skills/`` section
    (skill order = first reference across workflows, in name order).

    Caller contract (enforced by the export endpoints): names/versions come
    from DB rows, so per-scope uniqueness already holds. Raises ``ValueError``
    when two names slugify identically — the filename key cannot represent
    both.
    """
    slugs: dict[str, str] = {}
    prepared: list[tuple[WorkflowExport, str, str, list[tuple[str, str]]]] = []
    for wf in workflows:
        slug = slugify(wf.name)
        if slug in slugs:
            raise ValueError(
                f"workflow slug collision: '{slugs[slug]}' and '{wf.name}' "
                f"both map to '{slug}' — rename one before exporting"
            )
        slugs[slug] = wf.name

        pslugs: dict[str, str] = {}
        pdocs: list[tuple[str, str]] = []
        for p in sorted(wf.policies, key=lambda p: (p.name, p.version)):
            pslug = slugify(p.name)
            if pslug in pslugs:
                raise ValueError(
                    f"policy slug collision in workflow '{wf.name}': "
                    f"'{pslugs[pslug]}' and '{p.name}' both map to '{pslug}'"
                )
            pslugs[pslug] = p.name
            pdocs.append((
                pslug,
                _dump_wrapper(
                    p.yaml_content,
                    name=p.name,
                    version=p.version,
                    description=p.description,
                    is_active=p.is_active,
                ),
            ))

        manifest = _dump_wrapper(
            wf.yaml_content,
            name=wf.name,
            version=wf.version,
            description=wf.description,
            category=wf.category,
            is_active=wf.is_active,
        )
        prepared.append((wf, slug, manifest, pdocs))

    # Skill section: dedupe by name across workflows (first occurrence wins),
    # order = workflows in input order, then name order within each.
    skill_order: list[str] = []
    skill_pairs: dict[str, dict[str, str]] = {}
    for wf, _, _, _ in prepared:
        for sname in sorted(wf.skills):
            if sname not in skill_pairs:
                skill_pairs[sname] = wf.skills[sname]
                skill_order.append(sname)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _, slug, manifest, _ in prepared:
            info = zipfile.ZipInfo(f"{WORKFLOWS_DIR}/{slug}.yaml", _EPOCH)
            zf.writestr(info, manifest.encode("utf-8"))
        for _, slug, _, pdocs in prepared:
            for pslug, doc in pdocs:
                info = zipfile.ZipInfo(
                    f"{POLICIES_DIR}/{slug}/{pslug}.yaml", _EPOCH
                )
                zf.writestr(info, doc.encode("utf-8"))

        # Skills section: merge the canonical skill zip so the two exporters
        # can never drift (entries copied as-is — filenames, timestamps,
        # compression).
        skill_zip = build_skills_zip(
            [(name, skill_pairs[name]) for name in skill_order]
        )
        with zipfile.ZipFile(io.BytesIO(skill_zip)) as sz:
            for info in sz.infolist():
                zf.writestr(info, sz.read(info))

    return buf.getvalue()


# ── Import analysis ──────────────────────────────────────────────────────────


def _parse_wrapper(content: bytes) -> dict | None:
    """Wrapper YAML → dict, or None when malformed / not a mapping."""
    try:
        raw = yaml.safe_load(content.decode("utf-8", errors="replace"))
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_workflow_refs(yaml_content: str) -> tuple[bool, list[str]]:
    """``(ok, referenced skill names)`` — ok=False when malformed or missing
    required keys. Skill names fall back to the step id when ``skill:`` is
    absent, mirroring the engine's ``spec.get("skill", step_id)`` resolution.
    """
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return False, []
    if not isinstance(raw, dict) or "workflow" not in raw or "steps" not in raw:
        return False, []

    names: list[str] = []
    for step in raw.get("steps") or []:
        if not isinstance(step, dict):
            continue
        name = str(step.get("skill") or step.get("id") or "").strip()
        if name and name not in names:
            names.append(name)
    return True, names


def _policy_ok(yaml_content: str) -> bool:
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return False
    return isinstance(raw, dict) and "policy" in raw


def _stem(path: str) -> str:
    """Strip a ``.yaml``/``.yml`` extension from a filename."""
    return path[:-5] if path.endswith(".yaml") else path[:-4]


def analyze_zip(data: bytes) -> WorkflowZipAnalysis:
    """Parse + validate a workflows zip into a :class:`WorkflowZipAnalysis`.

    Raises :class:`ZipValidationError` for fatal problems: not a zip, empty
    zip, no ``workflows/`` section, unsafe paths, duplicate entry names,
    duplicate workflow ``(name, version)`` pairs, duplicate policy
    ``(name, version)`` pairs within one workflow, duplicate skill names in
    the ``skills/`` section, or a decompression-budget overrun. Malformed
    wrapper docs are non-fatal (``invalid_workflows`` / ``orphan_policies``
    rows), mirroring ``skill_import``'s ``invalid_dirs`` treatment.
    """
    entries = collect_entries(data)

    analysis = WorkflowZipAnalysis()

    # ── Classify sections (zip order preserved) ──
    workflow_files: dict[str, bytes] = {}  # rel path → content
    policy_files: dict[str, tuple[str, bytes]] = {}  # wslug/pslug.yaml → (wslug, content)
    has_skill_entries = False
    for name, content in entries.items():
        if name.startswith(WORKFLOWS_DIR + "/"):
            rel = name[len(WORKFLOWS_DIR) + 1:]
            if "/" in rel or not rel.endswith((".yaml", ".yml")):
                analysis.other_entries.append(name)
                continue
            workflow_files[rel] = content
        elif name.startswith(POLICIES_DIR + "/"):
            rest = name[len(POLICIES_DIR) + 1:]
            wslug, sep, pslug = rest.partition("/")
            if (
                not sep
                or not pslug
                or "/" in pslug
                or not pslug.endswith((".yaml", ".yml"))
            ):
                analysis.other_entries.append(name)
                continue
            policy_files[rest] = (wslug, content)
        elif name.startswith(SKILL_DIR + "/"):
            has_skill_entries = True
        else:
            analysis.other_entries.append(name)

    if not workflow_files:
        raise ZipValidationError(
            "The zip has no workflows/ folder with workflow files at its root"
        )

    # ── Workflows, in zip order ──
    slug_to_entry: dict[str, WorkflowEntry] = {}
    seen_workflows: set[tuple[str, int]] = set()
    for rel, content in workflow_files.items():
        path = f"{WORKFLOWS_DIR}/{rel}"
        raw = _parse_wrapper(content)
        if (
            raw is None
            or not isinstance(raw.get("name"), str)
            or not isinstance(raw.get("content"), str)
        ):
            analysis.invalid_workflows.append(path)
            continue
        name = raw["name"]
        version = _as_int(raw.get("version"), 1)
        if (name, version) in seen_workflows:
            raise ZipValidationError(
                f"duplicate workflow '{name}' v{version} in zip"
            )
        seen_workflows.add((name, version))

        category = raw.get("category")
        if not isinstance(category, str):
            category = None
        entry = WorkflowEntry(
            slug=_stem(rel),
            name=name,
            version=version,
            description=str(raw.get("description") or ""),
            category=category,
            is_active=(
                raw.get("is_active")
                if isinstance(raw.get("is_active"), bool)
                else True
            ),
            yaml_content=raw["content"],
        )
        ok, refs = _parse_workflow_refs(raw["content"])
        if not ok:
            entry.warnings.append(
                "workflow YAML unparseable — referenced skills unknown"
            )
        entry.referenced_skills = refs

        slug_to_entry[entry.slug] = entry
        slug_to_entry.setdefault(slugify(name), entry)  # either spelling resolves
        analysis.workflows.append(entry)

    # ── Skills section (shared classifier) ──
    if has_skill_entries:
        analysis.skills, analysis.invalid_skills = build_skill_bundles(entries)
    else:
        analysis.warnings.append("The zip carries no skills/ section")

    zip_skill_names = {b.name for b in analysis.skills}
    missing: list[str] = []
    for wf in analysis.workflows:
        for sname in wf.referenced_skills:
            if sname in zip_skill_names:
                continue
            wf.warnings.append(f"skill '{sname}' is not in the zip")
            if sname not in missing:
                missing.append(sname)
    analysis.missing_skills = sorted(missing)

    # ── Policies, matched to workflows by slug ──
    seen_policies: set[tuple[str, str, int]] = set()
    for rest, (wslug, content) in policy_files.items():
        path = f"{POLICIES_DIR}/{rest}"
        entry = slug_to_entry.get(wslug)
        raw = _parse_wrapper(content)
        if (
            entry is None
            or raw is None
            or not isinstance(raw.get("name"), str)
            or not isinstance(raw.get("content"), str)
        ):
            analysis.orphan_policies.append(path)
            continue
        pname = raw["name"]
        pversion = _as_int(raw.get("version"), 1)
        if (entry.name, pname, pversion) in seen_policies:
            raise ZipValidationError(
                f"duplicate policy '{pname}' v{pversion} for workflow "
                f"'{entry.name}' in zip"
            )
        seen_policies.add((entry.name, pname, pversion))

        pentry = PolicyEntry(
            slug=_stem(rest.partition("/")[2]),
            workflow_slug=entry.slug,
            name=pname,
            version=pversion,
            description=str(raw.get("description") or ""),
            is_active=(
                raw.get("is_active")
                if isinstance(raw.get("is_active"), bool)
                else True
            ),
            yaml_content=raw["content"],
        )
        if not _policy_ok(raw["content"]):
            pentry.warnings.append("policy YAML unparseable")
        entry.policies.append(pentry)

    return analysis
