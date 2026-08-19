"""Admin skill zip export — pure zip building (no FastAPI, no DB).

Turns (skill name, file map) pairs into a zip that round-trips through
``platform_api.skill_import.analyze_zip``: a ``skills/`` folder at the zip
root, one directory per skill containing ``SKILL.md`` plus supporting files.
The admin export endpoint validates everything up front (paths, SKILL.md
presence, import budgets) and this builder stays a dumb deterministic
writer: same input → byte-identical output.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable

from platform_api.skill_import import SKILL_DIR, SKILL_MD

# Pin all ZipInfo timestamps so exports are byte-deterministic — the import
# path ignores entry timestamps, so the round-trip is unaffected.
_EPOCH = (1980, 1, 1, 0, 0, 0)


def build_skills_zip(skills: Iterable[tuple[str, dict[str, str]]]) -> bytes:
    """Build an import-compatible skills zip from ``(name, {path: content})``.

    Entry layout: ``skills/<name>/<path>`` per file — ``SKILL.md`` first,
    then the remaining paths sorted (mirrors ``analyze_zip``'s per-bundle
    ordering). Skill order = input order.

    Caller contract (enforced by the export endpoint): every path is already
    normalized and safe (``_path_problem``), every skill has ``SKILL.md``,
    and the import budgets (per-file / total / entry-count) already hold.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, files in skills:
            paths = [SKILL_MD] + sorted(p for p in files if p != SKILL_MD)
            for path in paths:
                info = zipfile.ZipInfo(f"{SKILL_DIR}/{name}/{path}", _EPOCH)
                zf.writestr(info, files[path].encode("utf-8"))
    return buf.getvalue()
