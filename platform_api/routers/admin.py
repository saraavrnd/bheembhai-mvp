"""Admin API endpoints — platform-level user, project, and membership management.

All endpoints require the platform ADMIN role via ``require_admin``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from bheembhai.database import get_session
from bheembhai.models.project import Project
from bheembhai.models.user import Membership, User

from platform_api.dependencies import require_admin
from platform_api.schemas.admin import (
    MemberAdd,
    MemberResponse,
    MemberUpdate,
    MembershipBrief,
    ProjectCreateAdmin,
    ProjectResponseAdmin,
    ProjectUpdate,
    UpdatePlatformRole,
    UserResponse,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from bheembhai.protocols.auth import Identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _user_to_response(user: User, memberships: list[MembershipBrief] | None = None) -> UserResponse:
    return UserResponse(
        id=str(user.id),
        external_id=user.external_id,
        auth_provider=user.auth_provider,
        email=user.email,
        display_name=user.display_name,
        platform_role=user.platform_role,
        created_at=user.created_at.isoformat() if user.created_at else "",
        memberships=memberships or [],
    )


def _project_to_response(project, owner_name: str | None = None, member_count: int = 0) -> ProjectResponseAdmin:
    return ProjectResponseAdmin(
        id=str(project.id),
        name=project.name,
        owner_id=str(project.owner_id),
        owner_name=owner_name,
        member_count=member_count,
        created_at=project.created_at.isoformat() if project.created_at else "",
    )


def _member_to_response(membership, user_name: str = "", user_email: str = "") -> MemberResponse:
    return MemberResponse(
        id=str(membership.id),
        user_id=str(membership.user_id),
        user_name=user_name,
        user_email=user_email,
        role=membership.role,
        created_at=membership.created_at.isoformat() if membership.created_at else "",
    )


async def _get_project_or_404(project_id: str, db: "AsyncSession") -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return project


# ── Users ────────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[UserResponse]:
    """List all platform users with their project memberships."""
    users_result = await db.execute(
        select(User).order_by(User.created_at.desc())
    )
    users = users_result.scalars().all()

    # Batch-fetch memberships with project names
    from bheembhai.models.project import Project as ProjectModel
    memberships_result = await db.execute(
        select(Membership, ProjectModel.name)
        .join(ProjectModel, Membership.project_id == ProjectModel.id)
        .order_by(Membership.created_at)
    )
    # Group memberships by user_id
    user_memberships: dict[str, list[MembershipBrief]] = {}
    for m, pname in memberships_result.all():
        uid = str(m.user_id)
        if uid not in user_memberships:
            user_memberships[uid] = []
        user_memberships[uid].append(
            MembershipBrief(project_id=str(m.project_id), project_name=pname, role=m.role)
        )

    return [_user_to_response(u, user_memberships.get(str(u.id), [])) for u in users]


@router.patch("/users/{user_id}/role")
async def update_user_role(
    user_id: str,
    body: UpdatePlatformRole,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> UserResponse:
    """Promote or demote a user's platform role (ADMIN ↔ USER)."""
    if body.platform_role not in ("ADMIN", "USER"):
        raise HTTPException(400, "platform_role must be 'ADMIN' or 'USER'")

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, f"User {user_id} not found")

    # Prevent self-demotion
    admin_user, _ = _admin
    if str(user.id) == str(admin_user.id) and body.platform_role != "ADMIN":
        raise HTTPException(400, "Cannot demote your own admin role")

    user.platform_role = body.platform_role
    await db.commit()
    await db.refresh(user)

    logger.info("User %s platform_role updated to %s", user_id, body.platform_role)
    return _user_to_response(user)


# ── Projects ─────────────────────────────────────────────────────────────────


@router.get("/projects")
async def list_projects(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[ProjectResponseAdmin]:
    """List all projects with owner name and member count."""
    projects_result = await db.execute(
        select(Project).order_by(Project.created_at.desc())
    )
    projects = projects_result.scalars().all()

    responses: list[ProjectResponseAdmin] = []
    for project in projects:
        # Get owner name
        owner = await db.get(User, project.owner_id)
        owner_name = owner.display_name if owner else None

        # Get member count
        count_result = await db.execute(
            select(func.count(Membership.id)).where(Membership.project_id == project.id)
        )
        member_count = count_result.scalar() or 0

        responses.append(_project_to_response(project, owner_name, member_count))

    return responses


@router.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreateAdmin,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Create a new project and assign a project manager."""
    admin_user, _ = _admin

    # Determine the project manager: explicit user_id or the creating admin
    pm_id = body.project_manager_id or str(admin_user.id)
    pm_user = await db.get(User, pm_id)
    if pm_user is None:
        raise HTTPException(404, f"User {pm_id} not found")

    project = Project(
        name=body.name,
        owner_id=pm_user.id,
    )
    db.add(project)
    await db.flush()

    # Add the project manager as a member with project_manager role
    membership = Membership(
        user_id=pm_user.id,
        project_id=project.id,
        role="project_manager",
    )
    db.add(membership)
    await db.commit()
    await db.refresh(project)

    logger.info(
        "Project created: %s name=%s owner=%s pm=%s",
        project.id, body.name, pm_user.id, pm_user.display_name,
    )
    return _project_to_response(project, pm_user.display_name, member_count=1)


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Update a project's name."""
    project = await _get_project_or_404(project_id, db)

    if body.name is not None:
        project.name = body.name

    await db.commit()
    await db.refresh(project)

    owner = await db.get(User, project.owner_id)
    return _project_to_response(project, owner.display_name if owner else None)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete a project. CASCADE handles memberships, integrations, and workflows."""
    project = await _get_project_or_404(project_id, db)

    await db.delete(project)
    await db.commit()

    logger.info("Project deleted: %s name=%s", project_id, project.name)
    return Response(status_code=204)


# ── Memberships ──────────────────────────────────────────────────────────────


@router.get("/projects/{project_id}/members")
async def list_members(
    project_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[MemberResponse]:
    """List all members of a project with user details."""
    await _get_project_or_404(project_id, db)

    result = await db.execute(
        select(Membership, User.display_name, User.email)
        .join(User, Membership.user_id == User.id)
        .where(Membership.project_id == project_id)
        .order_by(Membership.created_at)
    )
    return [
        _member_to_response(m, user_name=name, user_email=email)
        for m, name, email in result.all()
    ]


@router.post("/projects/{project_id}/members", status_code=201)
async def add_member(
    project_id: str,
    body: MemberAdd,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> MemberResponse:
    """Add a user to a project with a specific role."""
    await _get_project_or_404(project_id, db)

    # Verify the user exists
    user = await db.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, f"User {body.user_id} not found")

    # Check for duplicate membership
    existing = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"User {body.user_id} is already a member of project {project_id}",
        )

    membership = Membership(
        user_id=user.id,
        project_id=project_id,
        role=body.role,
    )
    db.add(membership)
    await db.commit()
    await db.refresh(membership)

    logger.info(
        "Member added: user=%s project=%s role=%s",
        body.user_id, project_id, body.role,
    )
    return _member_to_response(membership, user.display_name, user.email)


@router.patch("/projects/{project_id}/members/{membership_id}")
async def update_member_role(
    project_id: str,
    membership_id: str,
    body: MemberUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> MemberResponse:
    """Change a member's project role."""
    await _get_project_or_404(project_id, db)

    membership = await db.get(Membership, membership_id)
    if membership is None or str(membership.project_id) != project_id:
        raise HTTPException(404, f"Membership {membership_id} not found in project {project_id}")

    membership.role = body.role
    await db.commit()
    await db.refresh(membership)

    user = await db.get(User, membership.user_id)
    logger.info(
        "Member role updated: membership=%s project=%s new_role=%s",
        membership_id, project_id, body.role,
    )
    return _member_to_response(
        membership,
        user_name=user.display_name if user else "",
        user_email=user.email if user else "",
    )


@router.delete("/projects/{project_id}/members/{membership_id}")
async def remove_member(
    project_id: str,
    membership_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Remove a member from a project."""
    await _get_project_or_404(project_id, db)

    membership = await db.get(Membership, membership_id)
    if membership is None or str(membership.project_id) != project_id:
        raise HTTPException(404, f"Membership {membership_id} not found in project {project_id}")

    await db.delete(membership)
    await db.commit()

    logger.info(
        "Member removed: membership=%s user=%s project=%s",
        membership_id, membership.user_id, project_id,
    )
    return Response(status_code=204)
