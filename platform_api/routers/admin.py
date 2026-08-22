"""Admin API endpoints — platform-level user, project, membership, skill, workflow, and policy management.

All endpoints require the platform ADMIN role via ``require_admin``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bheembhai.database import get_session, upsert_skill
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.models.run import Run
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import Membership, ProjectRole, User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.workflow_category import WorkflowCategory
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import selectinload

from platform_api.dependencies import require_admin
from platform_api.routers._skill_shared import (
    _get_skill_or_404,
    _skill_to_response,
    collect_skill_files,
    republish_skill,
)
from platform_api.routers._workflow_shared import (
    _parse_policy_yaml,
    _parse_workflow_yaml,
    _policy_to_response,
    _referenced_skill_names,
    _workflow_to_response,
    clone_referenced_skills,
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
    MembershipBrief,
    MemberUpdate,
    PolicyCreate,
    PolicyResponse,
    PolicyUpdate,
    ProjectCreateAdmin,
    ProjectResponseAdmin,
    ProjectUpdate,
    RoleResponse,
    SkillCreate,
    SkillExportRequest,
    SkillFileCreate,
    SkillFileResponse,
    SkillFileUpdate,
    SkillImportAnalyzeResponse,
    SkillImportResponse,
    SkillImportResult,
    SkillImportSkillAnalysis,
    SkillNameResponse,
    SkillResponse,
    SkillUpdate,
    TestConnectionResult,
    UpdatePlatformRole,
    UpdateUserEnabled,
    UserResponse,
    WorkflowCategoryCreate,
    WorkflowCategoryResponse,
    WorkflowCategoryUpdate,
    WorkflowCreate,
    WorkflowExportRequest,
    WorkflowImportAnalyzeResponse,
    WorkflowImportPolicyAnalysis,
    WorkflowImportResponse,
    WorkflowImportResult,
    WorkflowImportSkillAnalysis,
    WorkflowImportWorkflowAnalysis,
    WorkflowResponse,
    WorkflowUpdate,
)
from platform_api.skill_export import build_skills_zip
from platform_api.skill_import import (
    MAX_DECOMPRESSED_BYTES,
    MAX_ENTRY_COUNT,
    MAX_SINGLE_FILE_BYTES,
    MAX_UPLOAD_BYTES,
    ZipValidationError,
    analyze_zip,
    bundle_files_with_external,
)
from platform_api.workflow_zip import (
    PolicyExport,
    WorkflowExport,
    build_workflows_zip,
)
from platform_api.workflow_zip import (
    analyze_zip as analyze_workflow_zip,
)

if TYPE_CHECKING:
    from bheembhai.protocols.auth import Identity
    from sqlalchemy.ext.asyncio import AsyncSession

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
        description=project.description,
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


async def _get_project_or_404(project_id: str, db: AsyncSession) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return project


# ── Users ────────────────────────────────────────────────────────────────────


@router.get("/users")
async def list_users(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Create a new project and assign a project manager."""
    _admin_user, _ = _admin

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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> ProjectResponseAdmin:
    """Update a project's name and/or description."""
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

    if body.description is not None:
        project.description = body.description.strip()

    await db.commit()
    await db.refresh(project)

    owner = await db.get(User, project.owner_id)
    return _project_to_response(project, owner.display_name if owner else None)


@router.delete("/projects/{project_id}")
async def delete_project(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
):
    """Delete a project. DB FK CASCADE handles memberships, integrations, runs,
    workflows, policies, and project skills in a single DELETE (passive_deletes
    on the Project relationships — the ORM must not emulate the cascade)."""
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
# (_skill_to_response / _get_skill_or_404 live in _skill_shared.py — shared with
# the project-scoped PM router.)


@router.get("/skills")
async def list_skills(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> list[SkillResponse]:
    """List all skills with their files."""
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.project_id.is_(None))
        .order_by(Skill.name)
    )
    return [_skill_to_response(s) for s in result.scalars().unique().all()]


@router.get("/skills/names")
async def list_skill_names(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> list[SkillNameResponse]:
    """List skill IDs and names only (lightweight — no file contents)."""
    result = await db.execute(
        select(Skill.id, Skill.name)
        .where(Skill.project_id.is_(None))
        .order_by(Skill.name)
    )
    return [
        SkillNameResponse(id=str(row[0]), name=row[1])
        for row in result.all()
    ]


@router.get("/skills/{skill_id}")
async def get_skill(
    skill_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> SkillResponse:
    """Create a new skill."""
    # Check for duplicate name among platform skills (project skills are PM-owned)
    existing = (await db.execute(
        select(Skill).where(
            Skill.name == body.name.strip(), Skill.project_id.is_(None)
        )
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
    # Publish-on-write: stamp the S3 bundle (empty-skill bundle too — the
    # agent must download SOMETHING to run it).
    store = getattr(request.app.state, "object_store", None)
    if store is not None:
        await republish_skill(db, store, skill_id=str(skill.id))
        await db.commit()
    logger.info("Skill created: %s name=%s model=%s", skill.id, skill.name, skill.model)
    return _skill_to_response(skill)


@router.patch("/skills/{skill_id}")
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> SkillResponse:
    """Update skill metadata."""
    skill = await _get_skill_or_404(skill_id, db)

    if body.name is not None:
        name = body.name.strip()
        # Check for duplicate name among platform skills (excluding self)
        existing = (await db.execute(
            select(Skill).where(
                Skill.name == name,
                Skill.id != skill.id,
                Skill.project_id.is_(None),
            )
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
):
    """Delete a skill and all its files (CASCADE)."""
    skill = await _get_skill_or_404(skill_id, db)

    await db.delete(skill)
    await db.commit()

    logger.info("Skill deleted: %s name=%s", skill_id, skill.name)
    return Response(status_code=204)


# ── Skill zip import ──────────────────────────────────────────────────────────

_VALID_DECISIONS = {"import", "overwrite", "skip"}
_SKILL_NAME_MAX = 100  # mirrors SkillCreate.name max_length


async def _read_zip_upload(zip_file: UploadFile) -> bytes:
    """Chunked read with a 5 MiB cap — no body-size middleware exists, so the
    limit is enforced here on the bytes actually received."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await zip_file.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"Zip exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _existing_platform_skill_names(db: AsyncSession, names: list[str]) -> set[str]:
    """Names among *names* that already exist as platform skills."""
    if not names:
        return set()
    result = await db.execute(
        select(Skill.name).where(
            Skill.project_id.is_(None), Skill.name.in_(names)
        )
    )
    return set(result.scalars().all())


def _import_analysis_response(analysis, existing: set[str]) -> SkillImportAnalyzeResponse:
    """Bundle dataclasses → analysis-table response, with exists flags.

    ``files``/``file_contents`` cover everything the skill will import with:
    its own files plus zip-backed refs outside the skill dir (tagged by
    ``external_references`` in the UI).
    """
    return SkillImportAnalyzeResponse(
        skills=[
            SkillImportSkillAnalysis(
                name=b.name,
                directory=b.directory,
                description=b.description,
                model=b.model,
                compatibility=b.compatibility,
                warnings=b.warnings,
                files=list(b.files) + list(b.external_files),
                file_contents={**b.files, **b.external_files},
                missing_referenced=b.missing_referenced,
                external_references=b.external_references,
                exists=b.name in existing,
            )
            for b in analysis.skills
        ],
        invalid_dirs=analysis.invalid_dirs,
        other_entries=analysis.other_entries,
        warnings=analysis.warnings,
    )


@router.post("/skills/import/analyze")
async def analyze_skill_import(
    zip_file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> SkillImportAnalyzeResponse:
    """Analyze an uploaded skills zip (stateless — the zip is never stored).

    Returns one row per importable skill: name, dependent files, missing
    references, and whether a platform skill with that name already exists.
    """
    data = await _read_zip_upload(zip_file)
    try:
        analysis = analyze_zip(data)
    except ZipValidationError as exc:
        raise HTTPException(422, str(exc)) from None

    existing = await _existing_platform_skill_names(
        db, [b.name for b in analysis.skills]
    )
    logger.info(
        "Skill import analysis: %d skills, %d existing, %d invalid dirs",
        len(analysis.skills), len(existing), len(analysis.invalid_dirs),
    )
    return _import_analysis_response(analysis, existing)


@router.post("/skills/import")
async def import_skills(
    request: Request,
    zip_file: UploadFile = File(...),
    decisions: str = Form(...),
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> SkillImportResponse:
    """Apply per-skill import decisions from the analysis step.

    The zip is re-uploaded and re-validated (stateless two-phase flow); the
    decision keys must cover the analyzed name set exactly, catching a zip
    that changed between phases. Each skill runs in its own savepoint — one
    bad skill never kills the batch.
    """
    data = await _read_zip_upload(zip_file)
    try:
        analysis = analyze_zip(data)
    except ZipValidationError as exc:
        raise HTTPException(422, str(exc)) from None

    try:
        parsed = json.loads(decisions)
    except json.JSONDecodeError:
        raise HTTPException(422, "decisions must be a JSON object") from None
    if not isinstance(parsed, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
    ):
        raise HTTPException(422, "decisions must map skill names to actions")
    bad_actions = {k: v for k, v in parsed.items() if v not in _VALID_DECISIONS}
    if bad_actions:
        raise HTTPException(
            422, f"invalid decision action(s): {', '.join(sorted(bad_actions))}"
        )

    zip_names = {b.name for b in analysis.skills}
    decision_names = set(parsed)
    missing = sorted(zip_names - decision_names)
    unknown = sorted(decision_names - zip_names)
    if missing or unknown:
        raise HTTPException(
            422,
            "decisions must cover exactly the analyzed skills — "
            f"missing: {', '.join(missing) or 'none'}; "
            f"unknown: {', '.join(unknown) or 'none'}",
        )

    existing = await _existing_platform_skill_names(db, zip_names)
    results: list[SkillImportResult] = []

    for bundle in analysis.skills:
        action = parsed[bundle.name]
        if action == "skip":
            results.append(SkillImportResult(
                name=bundle.name, action=action, status="skipped",
            ))
            continue
        if len(bundle.name) > _SKILL_NAME_MAX:
            results.append(SkillImportResult(
                name=bundle.name, action=action, status="error",
                message=f"skill name exceeds {_SKILL_NAME_MAX} characters",
            ))
            continue
        try:
            async with db.begin_nested():
                if action == "import" and bundle.name in existing:
                    # Mirrors the 409 text of POST /skills — but as a per-skill
                    # error row so the rest of the batch still imports.
                    results.append(SkillImportResult(
                        name=bundle.name, action=action, status="error",
                        message=(
                            f"A skill named '{bundle.name}' already exists — "
                            "choose Overwrite"
                        ),
                    ))
                    continue
                files = bundle_files_with_external(bundle)
                skill = await upsert_skill(
                    db,
                    name=bundle.name,
                    description=bundle.description,
                    model=bundle.model,
                    compatibility=bundle.compatibility,
                    files=files,
                )
                # Publish-on-write inside the savepoint: a publish failure
                # rolls this skill's import back into the per-skill error row
                # without killing the batch.
                store = getattr(request.app.state, "object_store", None)
                if store is not None:
                    await republish_skill(db, store, skill_id=str(skill.id))
                status = "overwritten" if (
                    action == "overwrite" and bundle.name in existing
                ) else "imported"
                message = None
                if bundle.external_files:
                    message = (
                        f"included {len(bundle.external_files)} referenced "
                        "file(s) from outside the skill dir — SKILL.md "
                        "references updated"
                    )
                results.append(SkillImportResult(
                    name=bundle.name, action=action, status=status,
                    message=message, skill_id=str(skill.id),
                ))
        except Exception as exc:  # per-skill isolation is the point
            logger.exception("Skill import failed: %s", bundle.name)
            results.append(SkillImportResult(
                name=bundle.name, action=action, status="error",
                message=str(exc),
            ))

    await db.commit()

    summary = {"imported": 0, "overwritten": 0, "skipped": 0, "errors": 0}
    for result in results:
        # per-row status is "error" (singular); the summary bucket is plural
        summary["errors" if result.status == "error" else result.status] += 1
    logger.info("Skill import complete: %s", summary)
    return SkillImportResponse(results=results, summary=summary)


# ── Skill zip export ──────────────────────────────────────────────────────────


@router.post("/skills/export")
async def export_skills(
    body: SkillExportRequest,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> Response:
    """Zip selected platform skills, import-compatible and download-ready.

    The zip's layout round-trips through ``POST /skills/import`` —
    ``skills/<name>/<path>`` per file. Strict contract so the artifact is
    guaranteed re-deployable; export never silently drops anything:

    * unknown names → 404 (the selection must match the catalog exactly);
    * a skill without SKILL.md → 422 (a SKILL.md-less dir would vanish on
      re-import);
    * unsafe file paths (absolute, drive letter, ``..``) or collisions after
      normalization → 422 — import rejects the same zips as fatal;
    * import-budget overruns (per-file / total decompressed / entry count)
      → 422, so the exported zip is always re-importable.

    Export keys by DB ``Skill.name``; if a skill's stored SKILL.md frontmatter
    ``name:`` differs, re-import renames it — frontmatter wins on import.
    """
    names = list(dict.fromkeys(body.names))  # dedupe, preserve order
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.project_id.is_(None), Skill.name.in_(names))
    )
    by_name = {s.name: s for s in result.scalars().unique().all()}
    missing = sorted(set(names) - set(by_name))
    if missing:
        raise HTTPException(404, f"Skill(s) not found: {', '.join(missing)}")

    bundles: list[tuple[str, dict[str, str]]] = []
    total_bytes = 0
    total_entries = 0
    for name in sorted(by_name):
        ordered = collect_skill_files(by_name[name])
        total_bytes += sum(len(c.encode("utf-8")) for c in ordered.values())
        if total_bytes > MAX_DECOMPRESSED_BYTES:
            raise HTTPException(
                422,
                "export decompresses beyond the "
                f"{MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MB budget",
            )
        bundles.append((name, ordered))
        total_entries += len(ordered)
    if total_entries > MAX_ENTRY_COUNT:
        raise HTTPException(
            422, f"too many entries (max {MAX_ENTRY_COUNT})"
        )

    data = build_skills_zip(bundles)
    filename = f"bheembhai-skills-{datetime.now(timezone.utc):%Y%m%d}.zip"
    logger.info(
        "Skill export: %d skills, %d entries, %d bytes → %s",
        len(bundles), total_entries, len(data), filename,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# ── Workflow zip export/import ──────────────────────────────────────────────


async def _resolve_import_scope(db: AsyncSession, project_id: str | None) -> str | None:
    """Verify the project for project-scoped workflow import/export (404 on
    unknown). Returns the normalized scope: None → platform."""
    if project_id is None:
        return None
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return project_id


async def _existing_scoped_workflows(
    db: AsyncSession, project_id: str | None, pairs: list[tuple[str, int]]
) -> dict[tuple[str, int], Workflow]:
    """Existing workflows in scope keyed by (name, version)."""
    if not pairs:
        return {}
    stmt = select(Workflow).where(
        Workflow.project_id.is_(None)
        if project_id is None
        else Workflow.project_id == project_id
    )
    stmt = stmt.where(
        or_(*[
            and_(Workflow.name == n, Workflow.version == v) for n, v in pairs
        ])
    )
    result = await db.execute(stmt)
    return {(w.name, w.version): w for w in result.scalars().all()}


async def _existing_scoped_skill_names(
    db: AsyncSession, project_id: str | None, names: list[str]
) -> set[str]:
    """Names among *names* that already exist as skills in the scope."""
    if not names:
        return set()
    result = await db.execute(
        select(Skill.name).where(
            Skill.project_id.is_(None)
            if project_id is None
            else Skill.project_id == project_id,
            Skill.name.in_(names),
        )
    )
    return set(result.scalars().all())


async def _effective_skill(
    db: AsyncSession, project_id: str | None, name: str
) -> Skill | None:
    """The skill that actually runs for *name* in scope: project skills
    shadow platform skills (the engine's resolution), so export the
    project row when it exists and falls back to the platform row."""
    if project_id is not None:
        result = await db.execute(
            select(Skill)
            .options(selectinload(Skill.files))
            .where(Skill.project_id == project_id, Skill.name == name)
        )
        skill = result.scalars().first()
        if skill is not None:
            return skill
    result = await db.execute(
        select(Skill)
        .options(selectinload(Skill.files))
        .where(Skill.project_id.is_(None), Skill.name == name)
    )
    return result.scalars().first()


async def _upsert_skill_scoped(
    db: AsyncSession,
    *,
    project_id: str | None,
    name: str,
    description: str,
    model: str,
    compatibility: str | None,
    files: dict[str, str],
) -> Skill:
    """Upsert a skill in the given scope (project_id None → platform).

    Platform scope delegates to the shared ``upsert_skill``; project scope
    mirrors it with the same replace-file-set-exactly semantics — one helper
    so the two import paths cannot drift.
    """
    if project_id is None:
        return await upsert_skill(
            db,
            name=name,
            description=description,
            model=model,
            compatibility=compatibility,
            files=files,
        )
    result = await db.execute(
        select(Skill).where(Skill.project_id == project_id, Skill.name == name)
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        skill = Skill(
            project_id=project_id,
            name=name,
            description=description,
            model=model,
            compatibility=compatibility,
        )
        db.add(skill)
        await db.flush()
    else:
        skill.description = description
        skill.model = model
        skill.compatibility = compatibility

    existing_result = await db.execute(
        select(SkillFile).where(SkillFile.skill_id == skill.id)
    )
    existing_by_path: dict[str, SkillFile] = {
        f.path: f for f in existing_result.scalars().all()
    }
    for fpath, fcontent in files.items():
        ef = existing_by_path.pop(fpath, None)
        if ef is None:
            db.add(SkillFile(skill_id=skill.id, path=fpath, content=fcontent))
        else:
            ef.content = fcontent
    for stale in existing_by_path.values():
        await db.delete(stale)
    await db.flush()
    return skill


async def _policies_of(db: AsyncSession, workflow: Workflow) -> list[Policy]:
    result = await db.execute(select(Policy).where(Policy.workflow_id == workflow.id))
    return list(result.scalars().all())


async def _category_by_name(
    db: AsyncSession, name: str | None
) -> WorkflowCategory | None:
    """Category row by name (shared reference data across both scopes)."""
    if not name:
        return None
    result = await db.execute(
        select(WorkflowCategory).where(WorkflowCategory.name == name)
    )
    return result.scalar_one_or_none()


def _parse_workflow_decisions(decisions: str) -> dict[str, dict[str, str]]:
    """Validate the namespaced decisions JSON:
    ``{"workflows": {...}, "skills": {...}, "policies": {...}}`` — each
    section maps names to import|overwrite|skip (absent sections = empty)."""
    try:
        parsed = json.loads(decisions)
    except json.JSONDecodeError:
        raise HTTPException(422, "decisions must be a JSON object") from None
    if not isinstance(parsed, dict):
        raise HTTPException(422, "decisions must be a JSON object")

    out: dict[str, dict[str, str]] = {}
    for section in ("workflows", "skills", "policies"):
        sub = parsed.get(section, {})
        if not isinstance(sub, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in sub.items()
        ):
            raise HTTPException(
                422, f"decisions.{section} must map names to actions"
            )
        bad = {k: v for k, v in sub.items() if v not in _VALID_DECISIONS}
        if bad:
            raise HTTPException(
                422,
                f"invalid decision action(s) in {section}: "
                f"{', '.join(sorted(bad))}",
            )
        out[section] = sub
    return out


def _check_decision_coverage(
    section: str, zip_names: set[str], decision_names: set[str]
) -> None:
    missing = sorted(zip_names - decision_names)
    unknown = sorted(decision_names - zip_names)
    if missing or unknown:
        raise HTTPException(
            422,
            f"decisions.{section} must cover exactly the analyzed rows — "
            f"missing: {', '.join(missing) or 'none'}; "
            f"unknown: {', '.join(unknown) or 'none'}",
        )


async def _workflow_import_analysis_response(
    db: AsyncSession, project_id: str | None, analysis
) -> WorkflowImportAnalyzeResponse:
    """Zip analysis → analysis-table response with scope-aware exists flags."""
    wf_pairs = [(w.name, w.version) for w in analysis.workflows]
    existing_wfs = await _existing_scoped_workflows(db, project_id, wf_pairs)

    skill_names = [b.name for b in analysis.skills]
    existing_skills = await _existing_scoped_skill_names(db, project_id, skill_names)
    platform_skills: set[str] = set()
    if project_id is not None:
        result = await db.execute(
            select(Skill.name).where(
                Skill.project_id.is_(None), Skill.name.in_(skill_names)
            )
        )
        platform_skills = set(result.scalars().all())

    policy_rows: list[WorkflowImportPolicyAnalysis] = []
    for w in analysis.workflows:
        wf_row = existing_wfs.get((w.name, w.version))
        existing_policies = await _policies_of(db, wf_row) if wf_row else []
        for p in w.policies:
            policy_rows.append(WorkflowImportPolicyAnalysis(
                workflow=w.name,
                name=p.name,
                version=p.version,
                warnings=p.warnings,
                exists=any(
                    pol.name == p.name and pol.version == p.version
                    for pol in existing_policies
                ),
            ))

    return WorkflowImportAnalyzeResponse(
        workflows=[
            WorkflowImportWorkflowAnalysis(
                name=w.name,
                slug=w.slug,
                version=w.version,
                description=w.description,
                category=w.category,
                warnings=w.warnings,
                referenced_skills=w.referenced_skills,
                policy_names=[f"{p.name} v{p.version}" for p in w.policies],
                exists=(w.name, w.version) in existing_wfs,
            )
            for w in analysis.workflows
        ],
        skills=[
            WorkflowImportSkillAnalysis(
                name=b.name,
                description=b.description,
                model=b.model,
                warnings=b.warnings,
                files=list(b.files) + list(b.external_files),
                exists=b.name in existing_skills,
                platform_exists=b.name in platform_skills,
            )
            for b in analysis.skills
        ],
        policies=policy_rows,
        missing_skills=analysis.missing_skills,
        invalid_workflows=analysis.invalid_workflows,
        invalid_skills=analysis.invalid_skills,
        orphan_policies=analysis.orphan_policies,
        other_entries=analysis.other_entries,
        warnings=analysis.warnings,
    )


@router.post("/workflows/export")
async def export_workflows(
    body: WorkflowExportRequest,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> Response:
    """Zip selected workflows (platform or project scope), import-compatible.

    The zip round-trips through ``POST /workflows/import``: one
    ``workflows/<slug>.yaml`` manifest per workflow, its policies under
    ``policies/<slug>/``, and every referenced skill under ``skills/`` —
    project skills win, platform skills fill the gaps (the engine resolves
    the same way at run init). Unknown ids → 404; a workflow slug collision
    or an unexportable skill → 422; import budgets hold for the whole zip so
    the artifact is always re-importable.

    ``project_id`` pins the scope strictly. Without it (the platform list
    page shows every scope in one table), the selection may mix platform and
    project workflows: each workflow's skills then resolve in ITS OWN scope,
    and same-named entries whose scopes disagree fail with an actionable 422
    rather than shipping the wrong content.
    """
    await _resolve_import_scope(db, body.project_id)
    ids = list(dict.fromkeys(body.workflow_ids))  # dedupe, preserve order
    stmt = (
        select(Workflow)
        .options(selectinload(Workflow.policies))
        .where(Workflow.id.in_(ids))
    )
    if body.project_id is not None:
        stmt = stmt.where(Workflow.project_id == body.project_id)
    result = await db.execute(stmt)
    by_id = {str(w.id): w for w in result.scalars().unique().all()}
    missing = sorted(set(ids) - set(by_id))
    if missing:
        raise HTTPException(
            404, f"Workflow(s) not found in this scope: {', '.join(missing)}"
        )

    # Mixed-scope selection: a zip entry can hold one copy of a (name,
    # version) pair — a workflow shadowing a platform workflow of the same
    # name+version must be exported separately, not silently merged.
    name_scopes: dict[tuple[str, int], set[str]] = {}
    for wf in by_id.values():
        name_scopes.setdefault((wf.name, wf.version), set()).add(
            str(wf.project_id) if wf.project_id else "platform"
        )
    cross = [
        f"'{name}' v{version}"
        for (name, version), scopes in sorted(name_scopes.items())
        if len(scopes) > 1
    ]
    if cross:
        raise HTTPException(
            422,
            "workflow(s) "
            f"{', '.join(cross)} exist in more than one scope — the zip can "
            "hold one copy per name; uncheck the duplicates and export each "
            "scope separately",
        )

    exports: list[WorkflowExport] = []
    skill_content_by_name: dict[str, tuple[dict[str, str], str]] = {}
    total_bytes = 0
    total_entries = 0
    for wid in ids:
        wf = by_id[wid]
        if len(wf.yaml_content.encode("utf-8")) > MAX_SINGLE_FILE_BYTES:
            raise HTTPException(
                422,
                f"Workflow '{wf.name}': YAML too large (max "
                f"{MAX_SINGLE_FILE_BYTES // (1024 * 1024)} MB uncompressed)",
            )

        skills: dict[str, dict[str, str]] = {}
        for sname in sorted(
            _referenced_skill_names(_parse_workflow_yaml(wf.yaml_content))
        ):
            # Mixed scope: resolve in the workflow's OWN scope (project skill
            # shadows the platform skill, exactly like run init).
            skill = await _effective_skill(db, wf.project_id, sname)
            if skill is None:
                logger.warning(
                    "Workflow %s references skill '%s' missing from scope — "
                    "skipped in export",
                    wf.id, sname,
                )
                continue
            ordered = collect_skill_files(skill)
            prev = skill_content_by_name.get(sname)
            if prev is not None and prev[0] != ordered:
                raise HTTPException(
                    422,
                    f"skill '{sname}' resolves to different content for "
                    f"workflow '{prev[1]}' and workflow '{wf.name}' (their "
                    "scopes differ) — uncheck one of them and export each "
                    "scope separately",
                )
            if prev is None:
                skill_content_by_name[sname] = (ordered, wf.name)
            skills[sname] = ordered
            total_bytes += sum(len(c.encode("utf-8")) for c in ordered.values())
            if total_bytes > MAX_DECOMPRESSED_BYTES:
                raise HTTPException(
                    422,
                    "export decompresses beyond the "
                    f"{MAX_DECOMPRESSED_BYTES // (1024 * 1024)} MB budget",
                )
            total_entries += len(ordered)
            if total_entries > MAX_ENTRY_COUNT:
                raise HTTPException(422, f"too many entries (max {MAX_ENTRY_COUNT})")

        # manifests + policies count against the zip entry budget too
        total_entries += 1 + len(wf.policies)
        if total_entries > MAX_ENTRY_COUNT:
            raise HTTPException(422, f"too many entries (max {MAX_ENTRY_COUNT})")

        category = (
            await db.get(WorkflowCategory, wf.workflow_category_id)
            if wf.workflow_category_id
            else None
        )
        exports.append(WorkflowExport(
            name=wf.name,
            version=wf.version,
            yaml_content=wf.yaml_content,
            description=wf.description,
            category=category.name if category else None,
            is_active=wf.is_active,
            policies=[
                PolicyExport(
                    name=p.name,
                    version=p.version,
                    yaml_content=p.yaml_content,
                    is_active=p.is_active,
                )
                for p in wf.policies
            ],
            skills=skills,
        ))

    try:
        data = build_workflows_zip(exports)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    filename = f"bheembhai-workflows-{datetime.now(timezone.utc):%Y%m%d}.zip"
    logger.info(
        "Workflow export: %d workflows, %d entries, %d bytes → %s",
        len(exports), total_entries, len(data), filename,
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/workflows/import/analyze")
async def analyze_workflow_import(
    zip_file: UploadFile = File(...),
    project_id: str | None = Form(None),
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> WorkflowImportAnalyzeResponse:
    """Analyze an uploaded workflows zip (stateless — the zip is never stored).

    Returns one row per importable workflow / skill / policy with scope-aware
    exists flags (platform rows for platform scope; project rows — plus a
    ``platform_exists`` hint on skills — for project scope).
    """
    await _resolve_import_scope(db, project_id)
    data = await _read_zip_upload(zip_file)
    try:
        analysis = analyze_workflow_zip(data)
    except ZipValidationError as exc:
        raise HTTPException(422, str(exc)) from None

    logger.info(
        "Workflow import analysis: %d workflows, %d skills, %d policies",
        len(analysis.workflows), len(analysis.skills),
        sum(len(w.policies) for w in analysis.workflows),
    )
    return await _workflow_import_analysis_response(db, project_id, analysis)


_WORKFLOW_NAME_MAX = 100  # mirrors WorkflowCreate.name max_length


async def _import_one_workflow(
    db: AsyncSession,
    w,
    action: str,
    existing_wfs: dict[tuple[str, int], Workflow],
    project_id: str | None,
    results: list[WorkflowImportResult],
) -> Workflow | None:
    """One workflow row in its own savepoint; returns the row policies attach
    to (None when skipped/errored)."""
    if action == "skip":
        results.append(WorkflowImportResult(
            kind="workflow", name=w.name, action=action, status="skipped",
        ))
        return None
    if len(w.name) > _WORKFLOW_NAME_MAX:
        results.append(WorkflowImportResult(
            kind="workflow", name=w.name, action=action, status="error",
            message=f"workflow name exceeds {_WORKFLOW_NAME_MAX} characters",
        ))
        return None

    existing = existing_wfs.get((w.name, w.version))
    try:
        async with db.begin_nested():
            if action == "import" and existing is not None:
                # Mirrors the 409 text of POST /workflows — but as a per-row
                # error so the rest of the batch still imports.
                results.append(WorkflowImportResult(
                    kind="workflow", name=w.name, action=action, status="error",
                    message=(
                        f"A workflow named '{w.name}' v{w.version} already "
                        "exists — choose Overwrite"
                    ),
                ))
                return None
            if _parse_workflow_yaml(w.yaml_content) is None:
                results.append(WorkflowImportResult(
                    kind="workflow", name=w.name, action=action, status="error",
                    message="workflow YAML unparseable — cannot import",
                ))
                return None

            category = await _category_by_name(db, w.category)
            if category is None and w.category:
                category = WorkflowCategory(name=w.category, description="")
                db.add(category)
                await db.flush()

            if existing is not None:
                existing.yaml_content = w.yaml_content
                existing.description = w.description
                if category is not None:
                    existing.workflow_category_id = category.id
                # is_active untouched — overwriting never deactivates a live workflow
                row = existing
                status = "overwritten"
            else:
                row = Workflow(
                    project_id=project_id,
                    name=w.name,
                    version=w.version,
                    description=w.description,
                    yaml_content=w.yaml_content,
                    is_active=False,  # imported workflows land inactive
                    workflow_category_id=category.id if category else None,
                )
                db.add(row)
                await db.flush()
                status = "imported"
            results.append(WorkflowImportResult(
                kind="workflow", name=w.name, action=action, status=status,
                message="no category in the zip — workflow left uncategorized"
                if w.category is None else None,
                workflow_id=str(row.id),
            ))
            return row
    except Exception as exc:  # per-row isolation is the point
        logger.exception("Workflow import failed: %s", w.name)
        results.append(WorkflowImportResult(
            kind="workflow", name=w.name, action=action, status="error",
            message=str(exc),
        ))
        return None


async def _import_one_skill(
    request: Request,
    db: AsyncSession,
    bundle,
    action: str,
    project_id: str | None,
    existing_names: set[str],
    results: list[WorkflowImportResult],
) -> None:
    """One skill row in its own savepoint (mirrors the skill-import path)."""
    if action == "skip":
        results.append(WorkflowImportResult(
            kind="skill", name=bundle.name, action=action, status="skipped",
        ))
        return
    if len(bundle.name) > _SKILL_NAME_MAX:
        results.append(WorkflowImportResult(
            kind="skill", name=bundle.name, action=action, status="error",
            message=f"skill name exceeds {_SKILL_NAME_MAX} characters",
        ))
        return
    try:
        async with db.begin_nested():
            if action == "import" and bundle.name in existing_names:
                results.append(WorkflowImportResult(
                    kind="skill", name=bundle.name, action=action, status="error",
                    message=(
                        f"A skill named '{bundle.name}' already exists — "
                        "choose Overwrite"
                    ),
                ))
                return
            files = bundle_files_with_external(bundle)
            skill = await _upsert_skill_scoped(
                db,
                project_id=project_id,
                name=bundle.name,
                description=bundle.description,
                model=bundle.model,
                compatibility=bundle.compatibility,
                files=files,
            )
            # Publish-on-write inside the savepoint: a publish failure rolls
            # this skill's import back into a per-skill error row.
            store = getattr(request.app.state, "object_store", None)
            if store is not None:
                await republish_skill(db, store, skill_id=str(skill.id))
            status = "overwritten" if (
                action == "overwrite" and bundle.name in existing_names
            ) else "imported"
            message = None
            if bundle.external_files:
                message = (
                    f"included {len(bundle.external_files)} referenced "
                    "file(s) from outside the skill dir — SKILL.md "
                    "references updated"
                )
            results.append(WorkflowImportResult(
                kind="skill", name=bundle.name, action=action, status=status,
                message=message, skill_id=str(skill.id),
            ))
    except Exception as exc:
        logger.exception("Skill import failed: %s", bundle.name)
        results.append(WorkflowImportResult(
            kind="skill", name=bundle.name, action=action, status="error",
            message=str(exc),
        ))


async def _import_one_policy(
    db: AsyncSession,
    workflow_row: Workflow | None,
    existing_workflow: Workflow | None,
    workflow_name: str,
    p,
    action: str,
    project_id: str | None,
    results: list[WorkflowImportResult],
) -> None:
    """One policy row in its own savepoint.

    Attaches to the workflow row this batch produced, falling back to the
    workflow row that already exists in scope — the same row the analyze
    table computed the policy's exists flag against. Neither present → the
    workflow was skipped/errored without a row to attach to.
    """
    target = workflow_row or existing_workflow
    label = f"{workflow_name} :: {p.name}"
    if action == "skip":
        results.append(WorkflowImportResult(
            kind="policy", name=label, action=action, status="skipped",
        ))
        return
    if target is None:
        results.append(WorkflowImportResult(
            kind="policy", name=label, action=action, status="error",
            message=(
                f"workflow '{workflow_name}' not imported — import or "
                "overwrite its workflow first"
            ),
        ))
        return
    if _parse_policy_yaml(p.yaml_content) is None:
        results.append(WorkflowImportResult(
            kind="policy", name=label, action=action, status="error",
            message="policy YAML unparseable — cannot import",
        ))
        return

    existing_policy = next(
        (
            pol for pol in await _policies_of(db, target)
            if pol.name == p.name and pol.version == p.version
        ),
        None,
    )
    try:
        async with db.begin_nested():
            if action == "import" and existing_policy is not None:
                results.append(WorkflowImportResult(
                    kind="policy", name=label, action=action, status="error",
                    message=(
                        f"A policy named '{p.name}' v{p.version} already "
                        f"exists on '{workflow_name}' — choose Overwrite"
                    ),
                ))
                return
            if existing_policy is not None:
                existing_policy.yaml_content = p.yaml_content
                row = existing_policy
                status = "overwritten"
            else:
                row = Policy(
                    project_id=project_id,
                    workflow_id=target.id,
                    name=p.name,
                    version=p.version,
                    yaml_content=p.yaml_content,
                    is_active=False,  # imported policies land inactive
                )
                db.add(row)
                await db.flush()
                status = "imported"
            results.append(WorkflowImportResult(
                kind="policy", name=label, action=action, status=status,
                policy_id=str(row.id),
            ))
    except Exception as exc:
        logger.exception("Policy import failed: %s", label)
        results.append(WorkflowImportResult(
            kind="policy", name=label, action=action, status="error",
            message=str(exc),
        ))


@router.post("/workflows/import")
async def import_workflows(
    request: Request,
    zip_file: UploadFile = File(...),
    decisions: str = Form(...),
    project_id: str | None = Form(None),
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> WorkflowImportResponse:
    """Apply per-row import decisions from the analysis step.

    The zip is re-uploaded and re-validated (stateless two-phase flow); the
    decisions are namespaced — ``{"workflows": {...}, "skills": {...},
    "policies": {...}}`` — and each section must cover its analyzed name set
    exactly, catching a zip that changed between phases. Policy keys are
    ``"<workflow> :: <policy>"``. Workflows run first (policies attach to
    their rows), then skills, then policies; every row runs in its own
    savepoint so one bad row never kills the batch. Imported workflows and
    policies land ``is_active=False``; categories match by name and are
    created when missing.
    """
    await _resolve_import_scope(db, project_id)
    data = await _read_zip_upload(zip_file)
    try:
        analysis = analyze_workflow_zip(data)
    except ZipValidationError as exc:
        raise HTTPException(422, str(exc)) from None

    parsed = _parse_workflow_decisions(decisions)
    policy_keys = {
        f"{w.name} :: {p.name}"
        for w in analysis.workflows
        for p in w.policies
    }
    _check_decision_coverage(
        "workflows", {w.name for w in analysis.workflows},
        set(parsed["workflows"]),
    )
    _check_decision_coverage(
        "skills", {b.name for b in analysis.skills}, set(parsed["skills"]),
    )
    _check_decision_coverage("policies", policy_keys, set(parsed["policies"]))

    existing_wfs = await _existing_scoped_workflows(
        db, project_id, [(w.name, w.version) for w in analysis.workflows]
    )
    existing_skill_names = await _existing_scoped_skill_names(
        db, project_id, [b.name for b in analysis.skills]
    )

    results: list[WorkflowImportResult] = []

    # Workflows first — policies attach to the rows this batch produced.
    workflow_rows: dict[str, Workflow] = {}
    for w in analysis.workflows:
        row = await _import_one_workflow(
            db, w, parsed["workflows"][w.name], existing_wfs, project_id, results
        )
        if row is not None:
            workflow_rows[w.name] = row

    # Skills are independent of the workflow rows.
    for b in analysis.skills:
        await _import_one_skill(
            request, db, b, parsed["skills"][b.name], project_id,
            existing_skill_names, results,
        )

    for w in analysis.workflows:
        for p in w.policies:
            await _import_one_policy(
                db, workflow_rows.get(w.name),
                existing_wfs.get((w.name, w.version)),
                w.name, p,
                parsed["policies"][f"{w.name} :: {p.name}"], project_id, results,
            )

    await db.commit()

    summary = {"imported": 0, "overwritten": 0, "skipped": 0, "errors": 0}
    for result in results:
        # per-row status is "error" (singular); the summary bucket is plural
        summary["errors" if result.status == "error" else result.status] += 1
    logger.info("Workflow import complete: %s", summary)
    return WorkflowImportResponse(results=results, summary=summary)


# ── Skill Files ───────────────────────────────────────────────────────────────


@router.get("/skills/{skill_id}/files/{file_id}")
async def get_skill_file(
    skill_id: str,
    file_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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

    # Content changed → republish the bundle.
    store = getattr(request.app.state, "object_store", None)
    if store is not None:
        await republish_skill(db, store, skill_id=skill_id)
        await db.commit()

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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> SkillFileResponse:
    """Update a skill file's content."""
    await _get_skill_or_404(skill_id, db)

    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")

    sf.content = body.content
    await db.commit()
    await db.refresh(sf)

    # Content changed → republish the bundle.
    store = getattr(request.app.state, "object_store", None)
    if store is not None:
        await republish_skill(db, store, skill_id=skill_id)
        await db.commit()

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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
):
    """Delete a file from a skill."""
    await _get_skill_or_404(skill_id, db)

    sf = await db.get(SkillFile, file_id)
    if sf is None or str(sf.skill_id) != skill_id:
        raise HTTPException(404, f"File {file_id} not found in skill {skill_id}")

    await db.delete(sf)
    await db.commit()

    # Content changed → republish the bundle.
    store = getattr(request.app.state, "object_store", None)
    if store is not None:
        await republish_skill(db, store, skill_id=skill_id)
        await db.commit()

    logger.info("Skill file deleted: skill=%s path=%s", skill_id, sf.path)
    return Response(status_code=204)


# ── Roles ────────────────────────────────────────────────────────────────────


@router.get("/roles")
async def list_roles(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> list[RoleResponse]:
    """List all SDLC project roles (for policy editor dropdowns)."""
    from sqlalchemy import select as _select
    result = await db.execute(_select(ProjectRole).order_by(ProjectRole.key))
    return [
        RoleResponse(key=r.key, label=r.label)
        for r in result.scalars().all()
    ]


# ── Workflow categories ─────────────────────────────────────────────────────


def _category_to_response(category: WorkflowCategory) -> WorkflowCategoryResponse:
    return WorkflowCategoryResponse(
        id=str(category.id),
        name=category.name,
        description=category.description,
        created_at=category.created_at.isoformat() if category.created_at else "",
    )


async def _find_category_name_duplicate(
    db: AsyncSession, name: str, exclude_id=None
) -> WorkflowCategory | None:
    """Case-insensitive name duplicate (optionally excluding one row)."""
    stmt = select(WorkflowCategory).where(
        func.lower(WorkflowCategory.name) == name.lower()
    )
    if exclude_id is not None:
        stmt = stmt.where(WorkflowCategory.id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none()


@router.get("/workflow-categories")
async def list_workflow_categories(
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> list[WorkflowCategoryResponse]:
    """List all workflow categories (for workflow editor/create dropdowns)."""
    result = await db.execute(
        select(WorkflowCategory).order_by(WorkflowCategory.name)
    )
    return [_category_to_response(c) for c in result.scalars().all()]


@router.post("/workflow-categories", status_code=201)
async def create_workflow_category(
    body: WorkflowCategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> WorkflowCategoryResponse:
    """Create a workflow category."""
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Category name is required")
    if await _find_category_name_duplicate(db, name) is not None:
        raise HTTPException(409, f"A category named '{name}' already exists")

    category = WorkflowCategory(name=name, description=body.description.strip())
    db.add(category)
    await db.commit()

    logger.info("Workflow category created: %s name=%s", category.id, category.name)
    return _category_to_response(category)


@router.patch("/workflow-categories/{category_id}")
async def update_workflow_category(
    category_id: str,
    body: WorkflowCategoryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> WorkflowCategoryResponse:
    """Update a workflow category's name or description."""
    category = await db.get(WorkflowCategory, category_id)
    if category is None:
        raise HTTPException(404, f"Category {category_id} not found")

    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Category name is required")
        if await _find_category_name_duplicate(db, name, exclude_id=category.id) is not None:
            raise HTTPException(409, f"A category named '{name}' already exists")
        category.name = name
    if body.description is not None:
        category.description = body.description.strip()

    await db.commit()

    logger.info("Workflow category updated: %s", category_id)
    return _category_to_response(category)


@router.delete("/workflow-categories/{category_id}")
async def delete_workflow_category(
    category_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> Response:
    """Delete a workflow category. 409 when workflows still reference it —
    the admin must re-categorize (or clear) those workflows first.
    """
    category = await db.get(WorkflowCategory, category_id)
    if category is None:
        raise HTTPException(404, f"Category {category_id} not found")

    in_use = (
        await db.execute(
            select(func.count(Workflow.id)).where(
                Workflow.workflow_category_id == category.id
            )
        )
    ).scalar() or 0
    if in_use:
        raise HTTPException(
            409,
            f"Category '{category.name}' is used by {in_use} workflow(s) — "
            "re-categorize or clear them first",
        )

    await db.delete(category)
    await db.commit()

    logger.info("Workflow category deleted: %s name=%s", category_id, category.name)
    return Response(status_code=204)


# ── Workflows ───────────────────────────────────────────────────────────────


@router.get("/workflows")
async def list_workflows(
    request: Request,
    project_id: str | None = None,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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

    category = await db.get(WorkflowCategory, body.category_id)
    if category is None:
        raise HTTPException(400, f"Category {body.category_id} not found")

    workflow = Workflow(
        name=name,
        description=body.description.strip(),
        version=version,
        yaml_content=yaml_content,
        is_active=True,
        workflow_category_id=category.id,
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    if body.description is not None:
        workflow.description = body.description.strip()
    # Key present → set the category (clearing is rejected — workflows must
    # always belong to a category); absent → unchanged.
    if "category_id" in body.model_fields_set:
        if not body.category_id:
            raise HTTPException(
                400, "Category is required — workflows must belong to a category"
            )
        category = await db.get(WorkflowCategory, body.category_id)
        if category is None:
            raise HTTPException(400, f"Category {body.category_id} not found")
        workflow.workflow_category_id = category.id

    await db.commit()

    logger.info("Workflow updated: %s", workflow_id)
    return await _workflow_to_response(workflow, db)


@router.delete("/workflows/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> WorkflowResponse:
    """Clone a platform workflow (and its policies + referenced skills) to a
    specific project.

    The source workflow remains as a platform template.  The clone gets
    ``project_id`` set so the project can customise it independently; every
    platform skill its steps reference is cloned into project scope too
    (names the project already has are left untouched).
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
        description=source.description,
        version=source.version,
        yaml_content=source.yaml_content,
        is_active=True,
        workflow_category_id=source.workflow_category_id,
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

    # Clone referenced platform skills into project-scoped rows (shared helper
    # with the PM copy endpoint — they must not diverge). The store publishes
    # each fresh clone's bundle (publish-on-write).
    await clone_referenced_skills(
        db, source, body.project_id,
        store=getattr(request.app.state, "object_store", None),
    )

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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    _integration_to_response,
    _secure_storage,
    _test_integration_connection,
    validate_ai_vendor_config,
)


@router.get("/projects/{project_id}/integrations")
async def admin_list_integrations(
    project_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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

    # AI vendors must map all three model tiers before they can be used.
    # Validate the *effective* config: a label-only save on an existing
    # integration must not fail because body.config was omitted.
    effective_config = body.config if body.config else (existing.config if existing else {})
    validate_ai_vendor_config(body.type, effective_config)

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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
        # AI vendors must map all three model tiers before they can be used
        validate_ai_vendor_config(integration.type, body.config)
        integration.config = body.config

    await db.commit()
    await db.refresh(integration)
    return _integration_to_response(integration)


@router.delete("/projects/{project_id}/integrations/{integration_id}")
async def admin_delete_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
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
            logger.debug(
                "test_connection: secure storage fetch failed for ref=%s integration=%s",
                integration.credential_ref, integration_id, exc_info=True,
            )
            credential_value = ""

    if not credential_value:
        logger.debug(
            "test_connection: no credential available for integration=%s ref=%s",
            integration_id, integration.credential_ref or "<none>",
        )
        return TestConnectionResult(ok=False, message="No credential stored — please save an API token first.")

    result = await _test_integration_connection(integration, credential_value)

    # On successful test, update verified_at
    if result.ok:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        integration.verified_at = _dt.now(_tz.utc)
        await db.commit()

    return result
