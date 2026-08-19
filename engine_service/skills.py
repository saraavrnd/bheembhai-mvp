"""Run-scoped skill library — DB rows → S3 bundles (Phase 1).

Project skills shadow platform skills by name: a project's edit of a
platform template must win. ``effective_skill_map`` is the name → skill
resolution the engine validates workflows against and self-heals bundles
from at init; the step container downloads its ONE skill from the pinned
bundle key (``BB_SKILL_URL``) at launch — nothing is materialized to disk
here, and BheemBhai bundles beat repo-tracked ``.claude/skills`` (the
runner wipes that dir before extracting).
"""

from __future__ import annotations

import logging

from bheembhai.models.skill import Skill
from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
