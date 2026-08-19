"""Admin skill zip import — pure zip parsing and validation (no FastAPI, no DB).

Parses an uploaded skills zip into per-skill bundles: a ``skills/`` folder at
the zip root, one directory per skill containing ``SKILL.md`` plus supporting
files (arbitrary nesting), with zip-slip and zip-bomb guards. Returns a
``ZipAnalysis`` that the admin endpoints turn into an analysis table and, on
confirm, into platform Skill/SkillFile rows (via
``bheembhai.database.upsert_skill``).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

from bheembhai.database import parse_skill_frontmatter

# Upload cap (enforced by the endpoint's chunked read — no body-size
# middleware exists) and decompression budgets (zip-bomb defense: a 5 MB
# compressed payload can expand arbitrarily, so budgets are measured on the
# DECOMPRESSED content, defeating lying ZipInfo.file_size headers).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 10 * 1024 * 1024
MAX_ENTRY_COUNT = 2000

SKILL_DIR = "skills"
SKILL_MD = "SKILL.md"


class ZipValidationError(Exception):
    """Fatal zip problem — the message is user-facing (surfaced as 422 detail)."""


@dataclass
class SkillBundle:
    """One importable skill: metadata from frontmatter + files from the zip."""

    directory: str  # "skills/<dirname>" as it appears in the zip
    name: str  # frontmatter name, else dirname
    description: str
    model: str  # already mapped to high|medium|low
    compatibility: str | None
    warnings: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # rel path → content
    missing_referenced: list[str] = field(default_factory=list)
    external_references: list[str] = field(default_factory=list)  # in zip, outside the skill dir
    external_refs: dict[str, str] = field(default_factory=dict)  # raw doc form → resolved path
    external_files: dict[str, str] = field(default_factory=dict)  # resolved path → content
    exists: bool = False  # populated by the endpoint (DB query)


@dataclass
class ZipAnalysis:
    """Analysis table data: per-skill bundles plus non-importable extras."""

    skills: list[SkillBundle] = field(default_factory=list)  # zip order
    invalid_dirs: list[str] = field(default_factory=list)  # skills/<name> without SKILL.md
    other_entries: list[str] = field(default_factory=list)  # root entries outside skills/
    warnings: list[str] = field(default_factory=list)


def _normalize_entry_name(name: str) -> str:
    """Backslashes → '/', strip leading './' and repeated slashes."""
    name = name.replace("\\", "/")
    while name.startswith("./"):
        name = name[2:]
    while "//" in name:
        name = name.replace("//", "/")
    return name


def _path_problem(name: str) -> str | None:
    """Human-readable problem for an unsafe zip entry path, else None."""
    if name.startswith("/"):
        return f"entry has an absolute path: {name}"
    if re.match(r"^[A-Za-z]:", name):
        return f"entry has a Windows drive letter: {name}"
    if any(seg == ".." for seg in name.split("/")):
        return f"entry escapes the skill directory: {name}"
    return None


def analyze_zip(data: bytes) -> ZipAnalysis:
    """Parse + validate a skills zip into a :class:`ZipAnalysis`.

    Raises :class:`ZipValidationError` for fatal problems: not a zip, empty
    zip, no ``skills/`` folder, unsafe paths, duplicate entry names, duplicate
    resolved skill names, or a decompression-budget overrun. Skill dirs
    without ``SKILL.md`` are non-fatal (reported in ``invalid_dirs``).
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ZipValidationError("Not a valid zip file") from None

    with zf:
        infos = zf.infolist()
        if not infos:
            raise ZipValidationError("The zip is empty")
        if len(infos) > MAX_ENTRY_COUNT:
            raise ZipValidationError(
                f"The zip has too many entries (max {MAX_ENTRY_COUNT})"
            )

        # ── Pass 1: normalize + safety + budgets, collect file contents ──
        entries: dict[str, bytes] = {}
        total = 0
        for info in infos:
            name = _normalize_entry_name(info.filename)
            problem = _path_problem(name)
            if problem is not None:
                raise ZipValidationError(problem)
            if not name or name.endswith("/"):
                continue  # directory marker
            if name in entries:
                raise ZipValidationError(f"duplicate entry in zip: {name}")
            content = zf.read(info)
            if len(content) > MAX_SINGLE_FILE_BYTES:
                raise ZipValidationError(
                    f"file too large: {name} (max "
                    f"{MAX_SINGLE_FILE_BYTES // (1024 * 1024)} MB uncompressed)"
                )
            total += len(content)
            if total > MAX_DECOMPRESSED_BYTES:
                raise ZipValidationError(
                    f"zip decompresses beyond the "
                    f"{MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MB budget"
                )
            entries[name] = content

        # ── Pass 2: classify into skills/<dirname>/… bundles ──
        analysis = ZipAnalysis()
        skill_files: dict[str, dict[str, str]] = {}
        for name, content in entries.items():
            if not name.startswith(SKILL_DIR + "/"):
                analysis.other_entries.append(name)
                continue
            rest = name[len(SKILL_DIR) + 1:]
            dirname, _, rel = rest.partition("/")
            if not rel:
                continue  # `skills/<name>` itself (dir marker or stray file)
            skill_files.setdefault(dirname, {})[rel] = content.decode(
                "utf-8", errors="replace"
            )

        if not skill_files:
            raise ZipValidationError(
                "The zip has no skills/ folder with skill directories at its root"
            )

        # ── Pass 3: build bundles in zip order ──
        seen_names: set[str] = set()
        for dirname, files in skill_files.items():
            if SKILL_MD not in files:
                analysis.invalid_dirs.append(f"{SKILL_DIR}/{dirname}")
                continue

            skill_md = files[SKILL_MD]
            fm = parse_skill_frontmatter(skill_md, dirname)
            if fm is None:
                fm = parse_skill_frontmatter("", dirname)
                fm.warnings.append(
                    "SKILL.md frontmatter unparseable — using defaults"
                )
            if fm.name in seen_names:
                raise ZipValidationError(
                    f"duplicate skill name '{fm.name}' in zip"
                )
            seen_names.add(fm.name)
            if len(fm.name) > 100:  # Skill.name max_length — import will refuse it
                fm.warnings.append(
                    "skill name exceeds 100 characters — cannot be imported"
                )

            # SKILL.md first, then the rest sorted — stable for the UI table.
            ordered: dict[str, str] = {"SKILL.md": files.pop(SKILL_MD)}
            for path in sorted(files):
                ordered[path] = files[path]

            refs = scan_referenced_files(
                skill_md,
                set(ordered),
                directory=f"{SKILL_DIR}/{dirname}",
                zip_entries=set(entries),
            )

            analysis.skills.append(
                SkillBundle(
                    directory=f"{SKILL_DIR}/{dirname}",
                    name=fm.name,
                    description=fm.description,
                    model=fm.model,
                    compatibility=fm.compatibility,
                    warnings=fm.warnings,
                    files=ordered,
                    missing_referenced=refs.missing,
                    external_references=refs.external,
                    external_refs=refs.external_refs,
                    external_files={
                        path: entries[path].decode("utf-8", errors="replace")
                        for path in refs.external
                    },
                )
            )

    return analysis


_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_BARE_PATH_RE = re.compile(
    r"(?<![\w.])((?:\.\./)*(?:references|templates|examples|scripts|assets)/[\w.\-~+/]+)"
)


def _resolve_reference(ref: str, directory: str) -> str | None:
    """Resolve a reference relative to ``directory`` (e.g. ``skills/<name>``).

    Returns the repo-root-relative path, or None when the reference escapes
    the repository entirely (more leading ``..`` segments than levels above
    the repo root). Mid-path ``..`` segments are collapsed.
    """
    base = directory.split("/")
    segs = ref.split("/")
    up = 0
    while segs and segs[0] == "..":
        segs.pop(0)
        up += 1
    if up > len(base):
        return None  # escapes the repo root — external
    out: list[str] = []
    for seg in segs:
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            else:
                return None
            continue
        out.append(seg)
    resolved = base[: len(base) - up] + out
    return "/".join(resolved) if resolved else None


@dataclass
class RefScan:
    """SKILL.md reference scan: truly missing vs satisfied only by the zip."""

    missing: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    external_refs: dict[str, str] = field(default_factory=dict)  # raw → resolved


def scan_referenced_files(
    skill_md: str,
    present: set[str],
    directory: str,
    zip_entries: set[str] | None = None,
) -> RefScan:
    """Classify files referenced from SKILL.md.

    Scans markdown link targets and bare
    ``references|templates|examples|scripts|assets/…`` tokens (optionally
    ``../``-prefixed). References are resolved against the skill directory:

    * inside the skill directory → ``missing`` when absent from the bundle
      (path relative to the skill dir);
    * elsewhere in the repo (e.g. ``../../tools/REGISTRY.md``) → ``external``
      when the zip carries the file (satisfied — at import the file is
      brought into the skill and the reference rewritten, see
      :func:`bundle_files_with_external`) and ``missing`` (with a note) when
      the zip does not carry it;
    * outside the repo entirely → ignored.

    URLs, anchors, and absolute paths are ignored. Documented limitation:
    code fences are scanned too — this is a flagging aid, not a markdown
    parser.
    """
    zip_entries = set(zip_entries or ())
    referenced: set[str] = set()
    for match in _LINK_RE.finditer(skill_md):
        referenced.add(match.group(1))
    for match in _BARE_PATH_RE.finditer(skill_md):
        referenced.add(match.group(1))

    missing: set[str] = set()
    external: set[str] = set()
    external_refs: dict[str, str] = {}
    for ref in referenced:
        clean = ref.strip()
        if not clean:
            continue
        clean = clean.split("#", 1)[0]  # drop anchor
        while clean.startswith("./"):
            clean = clean[2:]
        if (
            not clean
            or "://" in clean
            or clean.startswith("/")
            or re.match(r"^[A-Za-z]:", clean)
        ):
            continue  # URL, anchor-only, or absolute — not ours
        resolved = _resolve_reference(clean, directory)
        if resolved is None:
            continue  # escapes the repository entirely
        if resolved.startswith(directory + "/"):
            rel = resolved[len(directory) + 1:]
            if rel not in present:
                missing.add(rel)
        elif resolved in zip_entries:
            external.add(resolved)
            external_refs[clean] = resolved  # exact doc form, for the rewrite
        else:
            missing.add(
                f"{resolved} (not importable — outside the skill directory)"
            )
    return RefScan(
        missing=sorted(missing), external=sorted(external),
        external_refs=external_refs,
    )


def bundle_files_with_external(bundle: SkillBundle) -> dict[str, str]:
    """The full file set for import: the skill's own files plus every
    referenced file that lives outside the skill dir but exists in the zip.

    External files are stored inside the skill at their repo-resolved path
    (e.g. ``tools/REGISTRY.md``) and the ``../../``-style references in
    SKILL.md are rewritten to that path, so the links keep resolving after
    import. This is the only layout the engine materializer will accept — it
    writes each skill's files under ``/skills/<skill_name>/`` and refuses any
    path escaping the skill dir — and it also resolves in the agent worktree,
    where the skill lands under ``.claude/skills/<skill_name>/``.

    Returns a new dict; the bundle is not mutated. When an external path
    collides with one of the skill's own files, the skill's own file wins.
    """
    if not bundle.external_files:
        return bundle.files

    files = dict(bundle.files)
    skill_md = files["SKILL.md"]
    for resolved, content in bundle.external_files.items():
        # Rewrite the reference to the new in-skill location: prefer the
        # exact raw forms the scan found in the doc (handles any number of
        # leading `../`), else any `../`-prefixed occurrence of the path.
        raws = [raw for raw, target in bundle.external_refs.items()
                if target == resolved]
        if raws:
            for raw in raws:
                skill_md = skill_md.replace(raw, resolved)
        else:
            pattern = re.compile(r"(\.\./)+" + re.escape(resolved))
            skill_md = pattern.sub(resolved, skill_md)
        files.setdefault(resolved, content)
    files["SKILL.md"] = skill_md
    return files
