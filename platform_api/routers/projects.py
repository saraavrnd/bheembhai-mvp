"""Project CRUD endpoints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from bheembhai.database import get_session
from bheembhai.models.project import Project
from bheembhai.models.user import Membership
from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_user
from platform_api.schemas.projects import ProjectCreate, ProjectResponse
from platform_api.users import get_or_create_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=str(project.id),
        name=project.name,
        owner_id=str(project.owner_id),
        created_at=project.created_at.isoformat(),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_projects(
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> list[ProjectResponse]:
    """List all projects the current user has access to."""
    current_user = await get_or_create_user(user, db)

    result = await db.execute(
        select(Project)
        .join(Membership, Membership.project_id == Project.id)
        .where(Membership.user_id == current_user.id)
        .order_by(Project.created_at.desc())
    )
    return [_to_response(row) for row in result.scalars().all()]


@router.post("", status_code=201)
async def create_project(
    body: ProjectCreate,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> ProjectResponse:
    """Create a new project. The creating user becomes the owner + first member."""
    current_user = await get_or_create_user(user, db)

    project = Project(
        name=body.name,
        owner_id=current_user.id,
    )
    db.add(project)
    await db.flush()

    # Add the creator as an admin member
    membership = Membership(
        user_id=current_user.id,
        project_id=project.id,
        role="admin",
    )
    db.add(membership)
    await db.commit()
    await db.refresh(project)

    logger.info("Project created: %s name=%s owner=%s", project.id, body.name, current_user.id)
    return _to_response(project)


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> ProjectResponse:
    """Get a single project by ID."""
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return _to_response(project)
