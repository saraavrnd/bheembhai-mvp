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
from platform_api.skill_import import (
    MAX_SINGLE_FILE_BYTES,
    _normalize_entry_name,
    _path_problem,
)

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


def collect_skill_files(skill: Skill) -> dict[str, str]:
    """Per-skill export validation → ordered ``{path: content}``.

    Raises HTTPException (422) when the skill has no SKILL.md, any file path
    is unsafe (absolute / drive letter / ``..``) or collides after
    normalization, or a file exceeds the import size budget. Shared by the
    skill and workflow export endpoints so their zip contracts never drift.
    """
    if not any(f.path == "SKILL.md" for f in skill.files):
        raise HTTPException(
            422, f"Skill '{skill.name}' has no SKILL.md — add files before exporting"
        )
    ordered: dict[str, str] = {}
    for f in sorted(skill.files, key=lambda f: f.path):
        path = _normalize_entry_name(f.path)
        problem = _path_problem(path)
        if problem is not None:
            raise HTTPException(422, f"Skill '{skill.name}': {problem}")
        if path in ordered:
            raise HTTPException(
                422,
                f"Skill '{skill.name}': files collide after path normalization: {path}",
            )
        size = len(f.content.encode("utf-8"))
        if size > MAX_SINGLE_FILE_BYTES:
            raise HTTPException(
                422,
                f"Skill '{skill.name}': file too large: {path} (max "
                f"{MAX_SINGLE_FILE_BYTES // (1024 * 1024)} MB uncompressed)",
            )
        ordered[path] = f.content
    return ordered


async def _get_skill_or_404(skill_id: str, db: AsyncSession) -> Skill:
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(404, f"Skill {skill_id} not found")
    return skill
