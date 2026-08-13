"""Lightweight reference data for authenticated users.

Small read-only lists that the PM configuration UI (and future member-facing
forms) need without admin privileges: SDLC roles and skill names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from bheembhai.database import get_session
from bheembhai.models.skill import Skill
from bheembhai.models.user import ProjectRole, User
from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_enabled_user
from platform_api.schemas.admin import RoleResponse, SkillNameResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api", tags=["reference-data"])


@router.get("/roles")
async def list_roles(
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[RoleResponse]:
    """List all SDLC project roles (for policy editor dropdowns)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    result = await db.execute(select(ProjectRole).order_by(ProjectRole.key))
    return [
        RoleResponse(key=r.key, label=r.label)
        for r in result.scalars().all()
    ]


@router.get("/skills/names")
async def list_skill_names(
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[SkillNameResponse]:
    """List skill IDs and names only (lightweight — no file contents)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    result = await db.execute(
        select(Skill.id, Skill.name).order_by(Skill.name)
    )
    return [
        SkillNameResponse(id=str(row[0]), name=row[1])
        for row in result.all()
    ]
