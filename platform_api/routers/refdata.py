"""Lightweight reference data for authenticated users.

Small read-only lists that the PM configuration UI (and future member-facing
forms) need without admin privileges: SDLC roles and skill names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select

from bheembhai.database import get_session
from bheembhai.models.skill import Skill
from bheembhai.models.user import Membership, ProjectRole, User
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
    project_id: str | None = Query(None),
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[SkillNameResponse]:
    """List skill IDs and names only (lightweight — no file contents).

    ``project_id=<uuid>`` (members only) → the union of platform skills and
    that project's skills; a project row shadows the platform row of the
    same name (its id is the one returned — the workflow editor's reference).
    No param → platform skills only.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    stmt = select(Skill.id, Skill.name, Skill.project_id)
    if project_id:
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == current_user.id,
                    Membership.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(403, "You are not a member of this project")
        # Union replaces the platform-only filter — chaining .where() would
        # AND the two and project rows could never match.
        stmt = stmt.where(
            or_(Skill.project_id.is_(None), Skill.project_id == project_id)
        )
    else:
        stmt = stmt.where(Skill.project_id.is_(None))

    result = await db.execute(stmt.order_by(Skill.name))
    # Dedupe by name — a project row shadows the platform row deterministically.
    by_name: dict[str, SkillNameResponse] = {}
    for row in result.all():
        existing = by_name.get(row[1])
        if existing is None or row[2] is not None:
            by_name[row[1]] = SkillNameResponse(id=str(row[0]), name=row[1])
    return list(by_name.values())
