"""Shared skill response builders — used by both the admin router and the
project-scoped (PM) router; keeps the routers from importing each other."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bheembhai.models.skill import Skill
from bheembhai.skill_publish import publish_skill
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from platform_api.schemas.admin import SkillFileResponse, SkillResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _skill_to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        id=str(skill.id),
        name=skill.name,
        description=skill.description,
        model=skill.model,
        compatibility=skill.compatibility,
        created_at=skill.created_at.isoformat() if skill.created_at else "",
        updated_at=skill.updated_at.isoformat() if skill.updated_at else "",
        files=[
            SkillFileResponse(
                id=str(f.id),
                path=f.path,
                content=f.content,
                created_at=f.created_at.isoformat() if f.created_at else "",
            )
            for f in (skill.files or [])
        ],
    )


async def republish_skill(db: AsyncSession, store, skill_id: str) -> None:
    """Repack + publish a skill's S3 bundle and stamp the row (Phase 1).

    Reloads the skill WITH files (async sessions can't lazy-load), publishes
    via ``bheembhai.skill_publish.publish_skill``, and flushes only — the
    caller commits so the DB row and the S3 object land together. Content is
    addressed, so re-publishing unchanged content is a head-check no-op.
    """
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill_id)
    )
    skill = result.scalars().first()
    if skill is None:
        return
    skill.s3_key, skill.sha256 = await publish_skill(store, skill)
    await db.flush()


async def _get_skill_or_404(skill_id: str, db: AsyncSession) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, f"Skill {skill_id} not found")
    return skill
