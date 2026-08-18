"""Shared skill response builders — used by both the admin router and the
project-scoped (PM) router; keeps the routers from importing each other."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException

from bheembhai.models.skill import Skill

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


async def _get_skill_or_404(skill_id: str, db: "AsyncSession") -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, f"Skill {skill_id} not found")
    return skill
