"""Admin API endpoints — platform-level user, project, membership, skill, workflow, and policy management.

All endpoints require the platform ADMIN role via ``require_admin``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import and_, func, select
from sqlalchemy.orm import joinedload, selectinload

from bheembhai.database import get_session
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import Membership, ProjectRole, User
from bheembhai.models.workflow import Policy, Workflow

from platform_api.dependencies import require_admin
from platform_api.routers._workflow_shared import (
    _parse_policy_yaml,
    _parse_workflow_yaml,
    _policy_to_response,
    _workflow_to_response,
)
from platform_api.schemas.admin import (
    CopyToProjectRequest,
    IntegrationAdminCreate,
    IntegrationAdminResponse,
    IntegrationAdminUpdate,
    IntegrationFieldDef,
    IntegrationTypeMeta,
    MemberAdd,
    MemberResponse,
    MemberUpdate,
    MembershipBrief,
    PolicyCreate,
    PolicyResponse,
    PolicyUpdate,
    ProjectCreateAdmin,
    ProjectResponseAdmin,
    ProjectUpdate,
    RoleResponse,
    SkillCreate,
    SkillFileCreate,
    SkillFileResponse,
    SkillFileUpdate,
    SkillNameResponse,
    SkillResponse,
    SkillUpdate,
    TestConnectionResult,
    UpdatePlatformRole,
    UpdateUserEnabled,
    UserResponse,
    WorkflowCreate,
    WorkflowResponse,
    WorkflowUpdate,
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
        is_enabled=user.is_enabled,
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


@router.patch("/users/{user_id}/enabled")
async def update_user_enabled(
    user_id: str,
    body: UpdateUserEnabled,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> UserResponse:
    """Enable or disable a user. Disabled users cannot log in."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, f"User {user_id} not found")

    # Prevent self-disable
    admin_user, _ = _admin
    if str(user.id) == str(admin_user.id) and not body.is_enabled:
        raise HTTPException(400, "Cannot disable your own account")

    user.is_enabled = body.is_enabled
    await db.commit()
    await db.refresh(user)

    action = "enabled" if body.is_enabled else "disabled"
    logger.info("User %s %s", user_id, action)
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


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Get a single project by ID."""
    project = await _get_project_or_404(project_id, db)

    owner = await db.get(User, project.owner_id)
    owner_name = owner.display_name if owner else None

    count_result = await db.execute(
        select(func.count(Membership.id)).where(Membership.project_id == project.id)
    )
    member_count = count_result.scalar() or 0

    return _project_to_response(project, owner_name, member_count)


@router.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreateAdmin,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Create a new project and assign a project manager."""
    admin_user, _ = _admin

    # Check for duplicate project name
    existing = (await db.execute(
        select(Project).where(Project.name == body.name.strip())
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"A project named '{body.name.strip()}' already exists")

    pm_user = await db.get(User, body.project_manager_id)
    if pm_user is None:
        raise HTTPException(404, f"User {body.project_manager_id} not found")

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
        name = body.name.strip()
        # Check for duplicate name (excluding self)
        existing = (await db.execute(
            select(Project).where(Project.name == name, Project.id != project.id)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, f"A project named '{name}' already exists")
        project.name = name

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
    project = await _get_project_or_404(project_id, db)

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
            f"{user.display_name} is already a member of '{project.name}'",
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


# ── Skills ────────────────────────────────────────────────────────────────────


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


@router.get("/skills")
async def list_skills(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[SkillResponse]:
    """List all skills with their files."""
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .order_by(Skill.name)
    )
    return [_skill_to_response(s) for s in result.scalars().unique().all()]


@router.get("/skills/names")
async def list_skill_names(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[SkillNameResponse]:
    """List skill IDs and names only (lightweight — no file contents)."""
    result = await db.execute(
        select(Skill.id, Skill.name).order_by(Skill.name)
    )
    return [
        SkillNameResponse(id=str(row[0]), name=row[1])
        for row in result.all()
    ]


@router.get("/skills/{skill_id}")
async def get_skill(
    skill_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillResponse:
    """Get a single skill with all files."""
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill_id)
    )
    skill = result.scalars().first()
    if skill is None:
        raise HTTPException(404, f"Skill {skill_id} not found")
    return _skill_to_response(skill)


@router.post("/skills", status_code=201)
async def create_skill(
    body: SkillCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillResponse:
    """Create a new skill."""
    # Check for duplicate name
    existing = (await db.execute(
        select(Skill).where(Skill.name == body.name.strip())
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"A skill named '{body.name.strip()}' already exists")

    skill = Skill(
        name=body.name.strip(),
        description=body.description,
        model=body.model,
        compatibility=body.compatibility,
    )
    db.add(skill)
    await db.commit()
    # Reload with files so the relationship is eagerly loaded (async can't lazy-load).
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill.id)
    )
    skill = result.scalars().first()
    logger.info("Skill created: %s name=%s model=%s", skill.id, skill.name, skill.model)
    return _skill_to_response(skill)


@router.patch("/skills/{skill_id}")
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillResponse:
    """Update skill metadata."""
    skill = await _get_skill_or_404(skill_id, db)

    if body.name is not None:
        name = body.name.strip()
        # Check for duplicate name (excluding self)
        existing = (await db.execute(
            select(Skill).where(Skill.name == name, Skill.id != skill.id)
        )).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(409, f"A skill named '{name}' already exists")
        skill.name = name
    if body.description is not None:
        skill.description = body.description
    if body.model is not None:
        skill.model = body.model
    if body.compatibility is not None:
        skill.compatibility = body.compatibility

    await db.commit()
    # Reload with files — refresh would expire the relationship into an
    # unloaded state that can't lazy-load in async mode.
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.id == skill_id)
    )
    skill = result.scalars().first()

    logger.info("Skill updated: %s", skill_id)
    return _skill_to_response(skill)


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete a skill and all its files (CASCADE)."""
    skill = await _get_skill_or_404(skill_id, db)

    await db.delete(skill)
    await db.commit()

    logger.info("Skill deleted: %s name=%s", skill_id, skill.name)
    return Response(status_code=204)


# ── Skill Files ───────────────────────────────────────────────────────────────


@router.get("/skills/{skill_id}/files/{file_id}")
async def get_skill_file(
    skill_id: str,
    file_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillFileResponse:
    """Get a single skill file with full content."""
    await _get_skill_or_404(skill_id, db)

    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")

    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.post("/skills/{skill_id}/files", status_code=201)
async def create_skill_file(
    skill_id: str,
    body: SkillFileCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillFileResponse:
    """Add a file to a skill."""
    await _get_skill_or_404(skill_id, db)

    # Check for duplicate path
    existing = (await db.execute(
        select(SkillFile).where(
            SkillFile.skill_id == skill_id,
            SkillFile.path == body.path.strip(),
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"File '{body.path}' already exists in this skill")

    sf = SkillFile(
        skill_id=skill_id,
        path=body.path.strip(),
        content=body.content,
    )
    db.add(sf)
    await db.commit()
    await db.refresh(sf)

    logger.info("Skill file added: skill=%s path=%s", skill_id, sf.path)
    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.patch("/skills/{skill_id}/files/{file_id}")
async def update_skill_file(
    skill_id: str,
    file_id: str,
    body: SkillFileUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> SkillFileResponse:
    """Update a skill file's content."""
    await _get_skill_or_404(skill_id, db)

    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")

    sf.content = body.content
    await db.commit()
    await db.refresh(sf)

    logger.info("Skill file updated: skill=%s path=%s", skill_id, sf.path)
    return SkillFileResponse(
        id=str(sf.id),
        path=sf.path,
        content=sf.content,
        created_at=sf.created_at.isoformat() if sf.created_at else "",
    )


@router.delete("/skills/{skill_id}/files/{file_id}")
async def delete_skill_file(
    skill_id: str,
    file_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete a file from a skill."""
    await _get_skill_or_404(skill_id, db)

    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")

    await db.delete(sf)
    await db.commit()

    logger.info("Skill file deleted: skill=%s path=%s", skill_id, sf.path)
    return Response(status_code=204)


# ── Roles ────────────────────────────────────────────────────────────────────


@router.get("/roles")
async def list_roles(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[RoleResponse]:
    """List all SDLC project roles (for policy editor dropdowns)."""
    from sqlalchemy import select as _select
    result = await db.execute(_select(ProjectRole).order_by(ProjectRole.key))
    return [
        RoleResponse(key=r.key, label=r.label)
        for r in result.scalars().all()
    ]


# ── Workflows ───────────────────────────────────────────────────────────────


@router.get("/workflows")
async def list_workflows(
    request: Request,
    project_id: str | None = None,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[WorkflowResponse]:
    """List workflows.

    - No ``project_id`` → all workflows (platform + every project).
    - ``project_id=__platform__`` → only platform templates (``project_id IS NULL``).
    - ``project_id=<uuid>`` → only workflows belonging to that project.
    """
    stmt = select(Workflow).order_by(Workflow.created_at.desc())

    if project_id == "__platform__":
        stmt = stmt.where(Workflow.project_id.is_(None))
    elif project_id:
        stmt = stmt.where(Workflow.project_id == project_id)

    workflows_result = await db.execute(stmt)
    workflows = workflows_result.scalars().all()

    return [await _workflow_to_response(w, db) for w in workflows]


@router.get("/workflows/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> WorkflowResponse:
    """Get a single workflow with parsed YAML and associated policies."""
    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")
    return await _workflow_to_response(workflow, db)


@router.get("/workflows/{workflow_id}/policies")
async def list_workflow_policies(
    workflow_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[PolicyResponse]:
    """List all policies associated with a workflow."""
    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    result = await db.execute(
        select(Policy).where(Policy.workflow_id == workflow_id).order_by(Policy.created_at.desc())
    )
    return [_policy_to_response(p, workflow.name) for p in result.scalars().all()]


@router.post("/workflows", status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> WorkflowResponse:
    """Create a new workflow.

    If ``yaml_content`` is provided it is used as-is.  Otherwise a minimal
    skeleton is generated from the workflow name so the user can build out
    steps and routing in the visual editor.
    """
    name = body.name.strip()
    yaml_content = body.yaml_content

    if not yaml_content:
        # Auto-generate a minimal skeleton with no steps — the user builds
        # them out in the visual editor.
        yaml_content = (
            f"workflow: {name}\n"
            f"version: 1\n"
            f"start: ''\n"
            f"steps: []\n"
        )

    parsed = _parse_workflow_yaml(yaml_content)
    version = parsed.version if parsed else 1

    existing = (
        await db.execute(
            select(Workflow).where(
                Workflow.name == name,
                Workflow.version == version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"A workflow named '{name}' version {version} already exists",
        )

    workflow = Workflow(
        name=name,
        version=version,
        yaml_content=yaml_content,
        is_active=True,
    )
    db.add(workflow)
    await db.commit()

    logger.info("Workflow created: %s name=%s", workflow.id, workflow.name)
    return await _workflow_to_response(workflow, db)


@router.patch("/workflows/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> WorkflowResponse:
    """Update a workflow's name, YAML content, or active status."""
    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    if body.name is not None:
        name = body.name.strip()
        # Check for duplicate (excluding self) — global uniqueness on (name, version)
        existing = (
            await db.execute(
                select(Workflow).where(
                    Workflow.name == name,
                    Workflow.version == workflow.version,
                    Workflow.id != workflow.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                409,
                f"A workflow named '{name}' version {workflow.version} already exists",
            )
        workflow.name = name
    if body.yaml_content is not None:
        workflow.yaml_content = body.yaml_content
        # Update version from parsed YAML if present
        parsed = _parse_workflow_yaml(body.yaml_content)
        if parsed:
            workflow.version = parsed.version
    if body.is_active is not None:
        workflow.is_active = body.is_active

    await db.commit()

    logger.info("Workflow updated: %s", workflow_id)
    return await _workflow_to_response(workflow, db)


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete a workflow and all associated policies and runs.

    Children are deleted explicitly because the FK columns
    (``runs.workflow_id``, ``runs.policy_id``, ``policies.workflow_id``)
    lack ``ON DELETE CASCADE`` at the database level.
    """
    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    # 1. Delete runs that reference this workflow (CASCADE takes care of
    #    steps, transitions, and work-queue items).
    runs_result = await db.execute(
        select(Run).where(Run.workflow_id == workflow.id)
    )
    for run in runs_result.scalars().all():
        await db.delete(run)

    # 2. Delete policies that reference this workflow.
    policies_result = await db.execute(
        select(Policy).where(Policy.workflow_id == workflow.id)
    )
    for pol in policies_result.scalars().all():
        await db.delete(pol)

    # 3. Now safe to delete the workflow itself.
    await db.delete(workflow)
    await db.commit()

    logger.info("Workflow deleted: %s name=%s", workflow_id, workflow.name)
    return Response(status_code=204)


@router.post("/workflows/{workflow_id}/copy-to-project", status_code=201)
async def copy_workflow_to_project(
    workflow_id: str,
    body: CopyToProjectRequest,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> WorkflowResponse:
    """Clone a platform workflow (and its policies) to a specific project.

    The source workflow remains as a platform template.  The clone gets
    ``project_id`` set so the project can customise it independently.
    """
    source = await db.get(Workflow, workflow_id)
    if source is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    project = await db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, f"Project {body.project_id} not found")

    # Check for duplicate (project_id, name, version) — project-scoped uniqueness
    existing = (
        await db.execute(
            select(Workflow).where(
                and_(
                    Workflow.project_id == body.project_id,
                    Workflow.name == source.name,
                    Workflow.version == source.version,
                )
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise HTTPException(
            409,
            f"A workflow named '{source.name}' version {source.version} already exists in project '{project.name}'",
        )

    # Clone the workflow
    clone = Workflow(
        project_id=body.project_id,
        name=source.name,
        version=source.version,
        yaml_content=source.yaml_content,
        is_active=True,
    )
    db.add(clone)
    await db.flush()

    # Clone associated policies
    policies_result = await db.execute(
        select(Policy).where(Policy.workflow_id == source.id)
    )
    for pol in policies_result.scalars().all():
        db.add(Policy(
            project_id=body.project_id,
            workflow_id=clone.id,
            name=pol.name,
            version=pol.version,
            yaml_content=pol.yaml_content,
            is_active=pol.is_active,
        ))

    await db.commit()

    logger.info(
        "Workflow cloned: %s → %s (project=%s)",
        source.id, clone.id, body.project_id,
    )
    return await _workflow_to_response(clone, db)


# ── Runs ────────────────────────────────────────────────────────────────────


@router.get("/runs")
async def list_runs(
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[dict]:
    """List all runs across the platform, newest first (lightweight)."""
    runs_result = await db.execute(
        select(Run).order_by(Run.created_at.desc()).limit(100)
    )
    return [
        {
            "id": str(r.id),
            "project_id": str(r.project_id),
            "workflow_id": str(r.workflow_id),
            "story_id": r.story_id,
            "state": r.state,
            "current_step": r.current_step,
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in runs_result.scalars().all()
    ]


# ── Policies ────────────────────────────────────────────────────────────────


@router.get("/policies/{policy_id}")
async def get_policy(
    policy_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> PolicyResponse:
    """Get a single policy with parsed YAML."""
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, f"Policy {policy_id} not found")

    workflow = await db.get(Workflow, policy.workflow_id)
    return _policy_to_response(policy, workflow.name if workflow else None)


@router.post("/policies", status_code=201)
async def create_policy(
    body: PolicyCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> PolicyResponse:
    """Create a new policy tied to a workflow."""
    # Verify workflow exists
    workflow = await db.get(Workflow, body.workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {body.workflow_id} not found")

    # Check for duplicate — global uniqueness on (workflow_id, name, version)
    parsed = _parse_policy_yaml(body.yaml_content)
    version = parsed.version if parsed else 1
    existing = (
        await db.execute(
            select(Policy).where(
                Policy.workflow_id == body.workflow_id,
                Policy.name == body.name.strip(),
                Policy.version == version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"A policy named '{body.name.strip()}' version {version} already exists for this workflow",
        )

    policy = Policy(
        project_id=None,  # project-independent — linked later via project management
        workflow_id=body.workflow_id,
        name=body.name.strip(),
        version=version,
        yaml_content=body.yaml_content,
        is_active=True,
    )
    db.add(policy)
    await db.commit()

    logger.info("Policy created: %s name=%s workflow=%s", policy.id, policy.name, body.workflow_id)
    return _policy_to_response(policy, workflow.name)


@router.patch("/policies/{policy_id}")
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> PolicyResponse:
    """Update a policy's YAML content or active status."""
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, f"Policy {policy_id} not found")

    if body.yaml_content is not None:
        policy.yaml_content = body.yaml_content
        parsed = _parse_policy_yaml(body.yaml_content)
        if parsed:
            policy.version = parsed.version
    if body.is_active is not None:
        policy.is_active = body.is_active

    await db.commit()

    workflow = await db.get(Workflow, policy.workflow_id)
    logger.info("Policy updated: %s", policy_id)
    return _policy_to_response(policy, workflow.name if workflow else None)


@router.delete("/policies/{policy_id}")
async def delete_policy(
    policy_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete a policy."""
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, f"Policy {policy_id} not found")

    await db.delete(policy)
    await db.commit()

    logger.info("Policy deleted: %s name=%s", policy_id, policy.name)
    return Response(status_code=204)


# ── Integrations ──────────────────────────────────────────────────────────────
# Type registry + SecureStorage accessor + status/response helpers are shared
# with the project-scoped (PM) integrations router.


from platform_api.routers._integration_shared import (
    INTEGRATION_TYPE_REGISTRY,
    _integration_status,
    _integration_to_response,
    _secure_storage,
    _test_integration_connection,
)


@router.get("/projects/{project_id}/integrations")
async def admin_list_integrations(
    project_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[IntegrationAdminResponse]:
    """List integrations for a project, filling in unconfigured slots.

    Returns one entry per known integration type — configured integrations
    from the DB plus ``unconfigured`` placeholders for missing types.
    """
    await _get_project_or_404(project_id, db)

    result = await db.execute(
        select(ProjectIntegration)
        .where(ProjectIntegration.project_id == project_id)
        .order_by(ProjectIntegration.created_at)
    )
    existing: dict[str, ProjectIntegration] = {
        row.type: row for row in result.scalars().all()
    }

    responses: list[IntegrationAdminResponse] = []
    for type_key in INTEGRATION_TYPE_REGISTRY:
        integ = existing.get(type_key)
        if integ is not None:
            responses.append(_integration_to_response(integ))
        else:
            # Placeholder for unconfigured type
            responses.append(IntegrationAdminResponse(
                type=type_key,
                status="unconfigured",
            ))

    return responses


@router.get("/projects/{project_id}/integrations/types")
async def admin_list_integration_types(
    request: Request,
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[IntegrationTypeMeta]:
    """Return the integration type registry (labels, icons, field definitions)."""
    return [
        IntegrationTypeMeta(
            key=meta["key"],
            label=meta["label"],
            category=meta["category"],
            icon=meta["icon"],
            description=meta.get("description", ""),
            fields=[f["name"] for f in meta["fields"]],
        )
        for meta in INTEGRATION_TYPE_REGISTRY.values()
    ]


@router.get("/projects/{project_id}/integrations/types/{type_key}/fields")
async def admin_get_integration_fields(
    project_id: str,
    type_key: str,
    request: Request,
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> list[IntegrationFieldDef]:
    """Return the field definitions for a specific integration type."""
    meta = INTEGRATION_TYPE_REGISTRY.get(type_key)
    if meta is None:
        raise HTTPException(404, f"Unknown integration type: {type_key}")
    return [
        IntegrationFieldDef(
            name=f["name"],
            label=f["label"],
            field_type=f.get("field_type", "text"),
            required=f.get("required", False),
            placeholder=f.get("placeholder", ""),
            options=f.get("options"),
        )
        for f in meta["fields"]
    ]


@router.post("/projects/{project_id}/integrations", status_code=201)
async def admin_create_integration(
    project_id: str,
    body: IntegrationAdminCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> IntegrationAdminResponse:
    """Create or overwrite an integration for a project.

    If an integration of the same type already exists it is updated in-place
    (idempotent upsert-like behaviour from the admin form).
    """
    project = await _get_project_or_404(project_id, db)

    if body.type not in INTEGRATION_TYPE_REGISTRY:
        raise HTTPException(400, f"Unknown integration type: {body.type}")

    # Check for existing integration of this type
    existing_result = await db.execute(
        select(ProjectIntegration).where(
            ProjectIntegration.project_id == project.id,
            ProjectIntegration.type == body.type,
        )
    )
    existing = existing_result.scalar_one_or_none()

    if existing is not None:
        # Update in-place
        if body.label:
            existing.label = body.label
        if body.config:
            existing.config = body.config
        if body.credential_value:
            secure = _secure_storage(request)
            ref = existing.credential_ref
            if not ref:
                ref = f"/bheembhai/{project_id}/{body.type}/default"
            await secure.put(
                ref=ref,
                value=body.credential_value,
                metadata={"project_id": project_id, "type": body.type, "label": existing.label},
            )
            existing.credential_ref = ref
        await db.commit()
        await db.refresh(existing)
        logger.info("Integration updated: %s type=%s", existing.id, body.type)
        return _integration_to_response(existing)

    # Create new — only touch SecureStorage if a credential was provided
    ref_path = f"/bheembhai/{project_id}/{body.type}/default"
    credential_value = body.credential_value or ""
    ref = ""
    if credential_value:
        secure = _secure_storage(request)
        ref = await secure.put(
            ref=ref_path,
            value=credential_value,
            metadata={"project_id": project_id, "type": body.type, "label": body.label},
        )

    integration = ProjectIntegration(
        project_id=project.id,
        type=body.type,
        label=body.label or body.type,
        credential_ref=ref,
        config=body.config,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)

    logger.info("Integration created: %s type=%s label=%s", integration.id, body.type, integration.label)
    return _integration_to_response(integration)


@router.patch("/projects/{project_id}/integrations/{integration_id}")
async def admin_update_integration(
    project_id: str,
    integration_id: str,
    body: IntegrationAdminUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> IntegrationAdminResponse:
    """Update an integration's label, config, or rotate its credential."""
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    if body.credential_value is not None and body.credential_value:
        secure = _secure_storage(request)
        ref = integration.credential_ref
        if not ref:
            ref = f"/bheembhai/{project_id}/{integration.type}/default"
        await secure.put(
            ref=ref,
            value=body.credential_value,
            metadata={"project_id": project_id, "type": integration.type, "label": integration.label},
        )
        integration.credential_ref = ref
        logger.info("Credential rotated for integration %s", integration_id)

    if body.label is not None:
        integration.label = body.label
    if body.config is not None:
        integration.config = body.config

    await db.commit()
    await db.refresh(integration)
    return _integration_to_response(integration)


@router.delete("/projects/{project_id}/integrations/{integration_id}")
async def admin_delete_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
):
    """Delete an integration and its stored credential."""
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    if integration.credential_ref:
        secure = _secure_storage(request)
        await secure.delete(integration.credential_ref)

    await db.delete(integration)
    await db.commit()

    logger.info("Integration deleted: %s type=%s", integration_id, integration.type)
    return Response(status_code=204)


@router.post("/projects/{project_id}/integrations/{integration_id}/test")
async def admin_test_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    _admin: tuple[User, "Identity"] = Depends(require_admin),
) -> TestConnectionResult:
    """Test connectivity for an integration.

    Attempts a lightweight authenticated API call based on integration type
    and updates ``verified_at`` on success.
    """
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    # Fetch the credential from SecureStorage
    credential_value = ""
    if integration.credential_ref:
        try:
            secure = _secure_storage(request)
            cred = await secure.get(integration.credential_ref)
            credential_value = cred.value if cred else ""
        except Exception:
            credential_value = ""

    if not credential_value:
        return TestConnectionResult(ok=False, message="No credential stored — please save an API token first.")

    result = await _test_integration_connection(integration, credential_value)

    # On successful test, update verified_at
    if result.ok:
        from datetime import datetime as _dt, timezone as _tz
        integration.verified_at = _dt.now(_tz.utc)
        await db.commit()

    return result
