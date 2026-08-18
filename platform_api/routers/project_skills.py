"""Project-scoped skill endpoints — PM-gated CRUD + file content editing.

Project skills shadow platform skills by name at run time (the engine
resolves project-first), so a project manager can customize what a mapped
workflow's skills do. Clone-on-map (``copy_workflow_to_project``) seeds the
rows; this router edits them. Platform templates stay admin-managed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import User
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from platform_api.dependencies import require_project_manager
from platform_api.routers._skill_shared import _skill_to_response
from platform_api.schemas.admin import (
    SkillCreate,
    SkillFileCreate,
    SkillFileResponse,
    SkillFileUpdate,
    SkillResponse,
    SkillUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/skills", tags=["project-skills"]
)


async def _get_project_skill_or_404(
    skill_id: str, project_id: str, db: AsyncSession
) -> Skill:
    """Skill by id, scoped to the project — other projects' rows are invisible."""
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill_id, Skill.project_id == project_id)
    )
    skill = result.scalars().first()
    if skill is None:
        raise HTTPException(404, f"Skill {skill_id} not found in project {project_id}")
    return skill


@router.get("")
async def list_project_skills(
    project_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> list[SkillResponse]:
    """List the project's skills with their files."""
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.project_id == project_id)
        .order_by(Skill.name)
    )
    return [_skill_to_response(s) for s in result.scalars().unique().all()]


@router.post("", status_code=201)
async def create_project_skill(
    body: SkillCreate,
    project_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillResponse:
    """Create a new project-scoped skill."""
    name = body.name.strip()
    existing = (
        await db.execute(
            select(Skill).where(
                Skill.project_id == project_id, Skill.name == name
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409, f"A skill named '{name}' already exists in this project"
        )

    skill = Skill(
        project_id=project_id,
        name=name,
        description=body.description,
        model=body.model,
        compatibility=body.compatibility,
    )
    db.add(skill)
    await db.commit()
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill.id)
    )
    skill = result.scalars().first()
    logger.info(
        "Project skill created: %s name=%s project=%s", skill.id, skill.name, project_id
    )
    return _skill_to_response(skill)


@router.get("/{skill_id}")
async def get_project_skill(
    project_id: str,
    skill_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillResponse:
    """Get a single project skill with all files."""
    return _skill_to_response(await _get_project_skill_or_404(skill_id, project_id, db))


@router.patch("/{skill_id}")
async def update_project_skill(
    skill_id: str,
    body: SkillUpdate,
    project_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillResponse:
    """Update project skill metadata.

    The name is read-only: it is the workflow's reference key — renaming here
    would silently break every step that references the skill.
    """
    if body.name is not None:
        raise HTTPException(400, "Skill name is read-only for project skills")
    skill = await _get_project_skill_or_404(skill_id, project_id, db)

    if body.description is not None:
        skill.description = body.description
    if body.model is not None:
        skill.model = body.model
    if body.compatibility is not None:
        skill.compatibility = body.compatibility

    await db.commit()
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill_id)
    )
    skill = result.scalars().first()
    logger.info("Project skill updated: %s", skill_id)
    return _skill_to_response(skill)


@router.delete("/{skill_id}")
async def delete_project_skill(
    skill_id: str,
    project_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
):
    """Delete a project skill and all its files (CASCADE).

    Runs then fall back to the platform skill of the same name at next init.
    """
    skill = await _get_project_skill_or_404(skill_id, project_id, db)
    await db.delete(skill)
    await db.commit()
    logger.info("Project skill deleted: %s name=%s project=%s", skill_id, skill.name, project_id)
    return Response(status_code=204)


# ── Skill files (content editing is the core editor operation) ────────────────


@router.get("/{skill_id}/files/{file_id}")
async def get_project_skill_file(
    project_id: str,
    skill_id: str,
    file_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillFileResponse:
    """Get a single project skill file with full content."""
    await _get_project_skill_or_404(skill_id, project_id, db)
    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")
    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.post("/{skill_id}/files", status_code=201)
async def create_project_skill_file(
    project_id: str,
    skill_id: str,
    body: SkillFileCreate,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillFileResponse:
    """Add a file to a project skill."""
    await _get_project_skill_or_404(skill_id, project_id, db)
    path = body.path.strip()
    existing = (
        await db.execute(
            select(SkillFile).where(
                SkillFile.skill_id == skill_id, SkillFile.path == path
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"File '{path}' already exists in this skill")

    sf = SkillFile(skill_id=skill_id, path=path, content=body.content)
    db.add(sf)
    await db.commit()
    await db.refresh(sf)
    logger.info("Project skill file added: skill=%s path=%s", skill_id, sf.path)
    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.patch("/{skill_id}/files/{file_id}")
async def update_project_skill_file(
    project_id: str,
    skill_id: str,
    file_id: str,
    body: SkillFileUpdate,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
) -> SkillFileResponse:
    """Update a project skill file's content."""
    await _get_project_skill_or_404(skill_id, project_id, db)
    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")
    sf.content = body.content
    await db.commit()
    await db.refresh(sf)
    logger.info("Project skill file updated: skill=%s path=%s", skill_id, sf.path)
    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.delete("/{skill_id}/files/{file_id}")
async def delete_project_skill_file(
    project_id: str,
    skill_id: str,
    file_id: str,
    _pm: tuple[User, Identity] = Depends(require_project_manager),
    db: AsyncSession = Depends(get_session),
):
    """Delete a file from a project skill."""
    await _get_project_skill_or_404(skill_id, project_id, db)
    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")
    await db.delete(sf)
    await db.commit()
    logger.info("Project skill file deleted: skill=%s path=%s", skill_id, sf.path)
    return Response(status_code=204)
