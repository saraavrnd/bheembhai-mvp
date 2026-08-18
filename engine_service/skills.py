"""Run-scoped skill library — DB rows → host dir → container ``/skills`` mount.

Project skills shadow platform skills by name: a project's edit of a
platform template must win. Both beat repo-tracked ``.claude/skills`` in the
agent worktree (``run_skill.sh`` force-symlinks the overlay when
``BB_SKILLS_DIR`` is set).

The engine materializes the FULL effective library to disk because the
``/skills`` bind mount REPLACES the image's baked copy — the set written
here is exactly what the step container sees.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bheembhai.models.skill import Skill

logger = logging.getLogger(__name__)


async def load_run_skills(session, project_id) -> tuple[list[Skill], list[Skill]]:
    """(project_skills, platform_skills) for a run's project.

    Duck-typed session seam: anything with ``execute(stmt).scalars()`` works,
    so unit tests don't need a database.
    """
    platform_stmt = (
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.project_id.is_(None))
    )
    platform_skills = list(
        (await session.execute(platform_stmt)).scalars().unique().all()
    )
    project_skills: list[Skill] = []
    if project_id:
        project_stmt = (
            select(Skill)
            .options(selectinload(Skill.files))
            .where(Skill.project_id == project_id)
        )
        project_skills = list(
            (await session.execute(project_stmt)).scalars().unique().all()
        )
    return project_skills, platform_skills


def effective_skill_map(project_skills: list[Skill],
                        platform_skills: list[Skill]) -> dict[str, Skill]:
    """Name → skill with project rows shadowing platform rows."""
    by_name: dict[str, Skill] = {s.name: s for s in platform_skills}
    for s in project_skills:
        by_name[s.name] = s
    return by_name


def materialize_skills(workdir, run_id, skills: dict[str, Skill]) -> Path:
    """Write the full effective library to ``<workdir>/skills/<run_id>/``.

    Wipe-then-write: init re-runs on every dispatch claim (idempotent), and
    PM edits must take effect at the next dispatch — stale files from a
    previous materialization must never survive. Files are chmod'd 0644 and
    dirs 0755 because bind mounts do not inherit the image's ``chmod -R
    a+rX /skills``.

    Returns the materialized root (the mount source path).
    """
    target = Path(workdir) / "skills" / str(run_id)
    if target.is_dir():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o755)

    for name, skill in sorted(skills.items()):
        skill_dir = target / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_dir.chmod(0o755)
        for f in skill.files or []:
            # Defense-in-depth: a file path must stay inside its skill dir
            # (paths originate from the DB and pass through a PM editor).
            path = (skill_dir / f.path).resolve()
            if not path.is_relative_to(skill_dir.resolve()):
                logger.warning(
                    "run skills: skipping path outside skill dir: %s/%s",
                    name, f.path,
                )
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content)
            path.chmod(0o644)

    # Intermediate dirs created by mkdir(parents=True) — walk + chmod so
    # everything is readable inside the container.
    for dirpath, dirnames, _files in os.walk(target):
        for d in dirnames:
            (Path(dirpath) / d).chmod(0o755)

    logger.info("materialized %d skills to %s", len(skills), target)
    return target
