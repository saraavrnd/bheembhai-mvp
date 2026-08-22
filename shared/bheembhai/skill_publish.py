"""Deterministic skill bundles for S3 delivery (Phase 1).

Skills live as DB rows (``Skill`` + ``SkillFile``). On every content change
the platform packs the skill into a tar.gz bundle, content-addressed as
``skills/<name>/<sha256>.tar.gz``, and stamps the row with key + sha. The
engine freezes the key onto the run's Step rows at init and hands the agent
container a fresh presigned GET per launch (``BB_SKILL_URL`` +
``BB_SKILL_SHA256`` env) — no AWS credentials ever reach the agent.

Determinism matters: identical content must pack to identical bytes so
re-publishing is a head-check no-op, and concurrent engines that both miss
the head check still land the same object at the same key.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import tarfile
from pathlib import PurePosixPath

from bheembhai.models.skill import Skill

logger = logging.getLogger(__name__)

BUNDLE_CONTENT_TYPE = "application/gzip"
SKILLS_PREFIX = "skills"


def skill_object_key(name: str, sha256: str) -> str:
    """Content-addressed object key for a skill bundle."""
    return f"{SKILLS_PREFIX}/{name}/{sha256}.tar.gz"


def _entry_name(name: str, path: str) -> str | None:
    """``<skill>/<file>`` archive name, or None when the path escapes.

    Lexical guard (no filesystem resolution — resolution depends on the host
    and would break determinism): absolute paths and ``..`` segments are
    rejected. Paths originate from the DB and pass through a PM editor, so
    this is defense in depth, matching the old materialize_skills guard.
    """
    parts = PurePosixPath(path).parts
    if not parts or PurePosixPath(path).is_absolute() or ".." in parts:
        return None
    return f"{name}/{'/'.join(parts)}"


def pack_skill(skill: Skill) -> bytes:
    """Pack a skill's files into a deterministic tar.gz (entries sorted by
    path, mtimes/owner zeroed in both tar and gzip headers)."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tar,
    ):
        for f in sorted(skill.files or [], key=lambda f: f.path):
            name = _entry_name(skill.name, f.path)
            if name is None:
                logger.warning(
                    "skill bundle: skipping path outside skill dir: %s/%s",
                    skill.name, f.path,
                )
                continue
            info = tarfile.TarInfo(name)
            info.mtime = 0
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            data = f.content.encode("utf-8")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def publish_skill(store, skill: Skill) -> tuple[str, str]:
    """Pack + upload (skipping an identical existing object) and return
    ``(object_key, sha256)``. Idempotent: content-addressed, so concurrent
    publishers of the same content converge on the same key."""
    data = pack_skill(skill)
    sha = hashlib.sha256(data).hexdigest()
    key = skill_object_key(skill.name, sha)
    if await store.head(key) is None:
        await store.put(key, data, content_type=BUNDLE_CONTENT_TYPE)
    return key, sha
