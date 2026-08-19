"""Project CRUD endpoints."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.models.project import Project
from bheembhai.models.run import Run, Transition
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.workflow_category import WorkflowCategory
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from platform_api.dependencies import (
    get_current_enabled_user,
    require_project_manager,
    require_project_member,
)
from platform_api.routers._run_stats import _relative_time
from platform_api.routers._workflow_shared import (
    _parse_policy_yaml,
    _parse_workflow_yaml,
)
from platform_api.routers.runs import TERMINAL_RUN_STATES, _compute_elapsed
from platform_api.schemas.admin import MemberAdd, MemberResponse, MemberUpdate
from platform_api.schemas.projects import ProjectCreate, ProjectResponse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["projects"])


# ── Schemas ──────────────────────────────────────────────────────────────────


class ProjectUpdate(BaseModel):
    """Fields a project manager can update; renaming requires platform ADMIN."""
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=500)


class MyRoleResponse(BaseModel):
    project_id: str
    role: str
    role_label: str


class MemberCandidateResponse(BaseModel):
    id: str
    name: str
    email: str


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=str(project.id),
        name=project.name,
        description=project.description,
        owner_id=str(project.owner_id),
        created_at=project.created_at.isoformat(),
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_projects(
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[ProjectResponse]:
    """List all projects the current user has access to."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

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
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> ProjectResponse:
    """Create a new project. The creating user becomes the owner + first member."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

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
        role="project_manager",
    )
    db.add(membership)
    await db.commit()
    await db.refresh(project)

    logger.info("Project created: %s name=%s owner=%s", project.id, body.name, current_user.id)
    return _to_response(project)


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> ProjectResponse:
    """Get a single project by ID."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return _to_response(project)


@router.get("/{project_id}/my-role")
async def get_my_role(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> MyRoleResponse:
    """Return the current user's role in this project."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    # Verify project exists
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")

    membership_result = await db.execute(
        select(Membership).where(
            Membership.user_id == current_user.id,
            Membership.project_id == project_id,
        )
    )
    membership = membership_result.scalar_one_or_none()
    if membership is None:
        # Platform ADMINs bypass membership and act as project managers
        if current_user.platform_role == "ADMIN":
            return MyRoleResponse(
                project_id=project_id,
                role="project_manager",
                role_label="Project Manager",
            )
        raise HTTPException(403, "You are not a member of this project")

    role_labels = {
        "project_manager": "Project Manager",
        "developer": "Developer",
        "qa": "QA Engineer",
        "viewer": "Viewer",
    }
    return MyRoleResponse(
        project_id=project_id,
        role=membership.role,
        role_label=role_labels.get(membership.role, membership.role),
    )


@router.get("/{project_id}/overview")
async def get_project_overview(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Dashboard overview for a project: member count, workflow/policy names,
    and the policy gates configured in this project (for the role rail)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")

    if current_user.platform_role != "ADMIN":
        # Verify membership (platform ADMINs bypass)
        member_check = await db.execute(
            select(Membership).where(
                Membership.user_id == current_user.id,
                Membership.project_id == project_id,
            )
        )
        if member_check.scalar_one_or_none() is None:
            raise HTTPException(403, "You are not a member of this project")

    import yaml as _yaml

    def _yaml_dict(content: str | None) -> dict:
        if not content:
            return {}
        try:
            raw = _yaml.safe_load(content)
        except _yaml.YAMLError:
            return {}
        return raw if isinstance(raw, dict) else {}

    from sqlalchemy import func, or_

    member_count = (await db.execute(
        select(func.count()).select_from(Membership).where(Membership.project_id == project_id)
    )).scalar_one()

    # Workflows usable in this project: project-scoped + platform templates
    wf_result = await db.execute(
        select(Workflow)
        .where(
            Workflow.is_active == True,
            or_(Workflow.project_id == project_id, Workflow.project_id.is_(None)),
        )
        .order_by(Workflow.created_at.desc())
    )
    workflows = wf_result.scalars().all()

    # Gates from the active policies attached to those workflows
    gates: list[dict] = []
    policy_names: list[str] = []
    for wf in workflows:
        wf_def = _yaml_dict(wf.yaml_content or "")
        step_labels = {
            s.get("id", ""): s.get("label", s.get("id", ""))
            for s in (wf_def.get("steps") or [])
            if isinstance(s, dict)
        }
        pol_result = await db.execute(
            select(Policy).where(Policy.workflow_id == wf.id, Policy.is_active == True)
        )
        for p in pol_result.scalars().all():
            policy_names.append(p.name)
            pol_def = _yaml_dict(p.yaml_content or "")
            for step_id, g in (pol_def.get("gates") or {}).items():
                if not isinstance(g, dict):
                    continue
                gates.append({
                    "step_id": str(step_id),
                    "label": step_labels.get(str(step_id), str(step_id)),
                    "review": str(g.get("review", "required")),
                    "role": str(g.get("role", "any")),
                    "workflow_name": wf.name,
                    "policy_name": p.name,
                })

    # Dedupe display names (a project-scoped workflow can share a name
    # with a platform template) while preserving order
    seen_names: set[str] = set()
    workflow_names: list[str] = []
    for w in workflows:
        label = f"{w.name} v{w.version}"
        if label not in seen_names:
            seen_names.add(label)
            workflow_names.append(label)

    return {
        "project": {"id": project_id, "name": project.name},
        "member_count": member_count,
        "workflow_names": workflow_names,
        "policy_names": sorted(set(policy_names)),
        "gates": gates,
    }


@router.get("/{project_id}/workflow-catalog")
async def get_workflow_catalog(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> dict:
    """Workflows tab catalog: categories, workflow cards, and recent runs.

    One payload for the whole tab — all counts are grouped queries (never
    per-row count lookups, unlike ``_workflow_to_response``). The client
    filters by the selected category.
    """
    # Active project-scoped workflows only (inactive ones live in Configuration).
    wf_result = await db.execute(
        select(Workflow)
        .where(Workflow.project_id == project_id, Workflow.is_active == True)
        .order_by(Workflow.name)
    )
    workflows = wf_result.scalars().all()
    wf_ids = [w.id for w in workflows]

    # Categories (one IN query, name-ordered like the refdata endpoint).
    cat_ids = {w.workflow_category_id for w in workflows if w.workflow_category_id}
    cat_by_id: dict = {}
    if cat_ids:
        cat_result = await db.execute(
            select(WorkflowCategory)
            .where(WorkflowCategory.id.in_(cat_ids))
            .order_by(WorkflowCategory.name)
        )
        cat_by_id = {c.id: c for c in cat_result.scalars().all()}

    # Policy counts (grouped) and gate counts (from the active policies).
    policy_counts: dict = {}
    if wf_ids:
        pc_result = await db.execute(
            select(Policy.workflow_id, func.count(Policy.id))
            .where(Policy.workflow_id.in_(wf_ids))
            .group_by(Policy.workflow_id)
        )
        policy_counts = dict(pc_result.all())

    gate_counts: dict = {}
    if wf_ids:
        pol_result = await db.execute(
            select(Policy).where(
                Policy.workflow_id.in_(wf_ids), Policy.is_active == True
            )
        )
        for pol in pol_result.scalars().all():
            parsed = _parse_policy_yaml(pol.yaml_content)
            if parsed:
                gate_counts[pol.workflow_id] = (
                    gate_counts.get(pol.workflow_id, 0) + len(parsed.gates)
                )

    # In-flight (non-terminal) runs per workflow.
    in_flight: dict = {}
    if wf_ids:
        if_result = await db.execute(
            select(Run.workflow_id, func.count(Run.id))
            .where(Run.project_id == project_id, Run.state.not_in(TERMINAL_RUN_STATES))
            .group_by(Run.workflow_id)
        )
        in_flight = dict(if_result.all())

    # Last run per workflow (newest first) + its finished ts for finished runs.
    last_runs: dict = {}
    finished_ts: dict = {}
    if wf_ids:
        lr_result = await db.execute(
            select(Run)
            .where(Run.project_id == project_id, Run.workflow_id.in_(wf_ids))
            .order_by(Run.workflow_id, Run.created_at.desc())
            .distinct(Run.workflow_id)
        )
        last_runs = {r.workflow_id: r for r in lr_result.scalars().all()}
        last_run_ids = [r.id for r in last_runs.values()]
        if last_run_ids:
            tr_result = await db.execute(
                select(Transition).where(Transition.run_id.in_(last_run_ids))
            )
            for tr in tr_result.scalars().all():
                run = last_runs.get(tr.run_id)
                if run is None or run.state not in TERMINAL_RUN_STATES:
                    continue
                if not tr.step_id and tr.to_state in TERMINAL_RUN_STATES:
                    finished_ts[tr.run_id] = float(tr.ts)

    # Workflow cards.
    wf_cards: list[dict] = []
    for wf in workflows:
        parsed = _parse_workflow_yaml(wf.yaml_content)
        cat = cat_by_id.get(wf.workflow_category_id) if wf.workflow_category_id else None
        last_run: dict | None = None
        lr = last_runs.get(wf.id)
        if lr is not None:
            ts = finished_ts.get(lr.id)
            last_run = {
                "run_id": str(lr.id),
                "story_id": lr.story_id,
                "state": lr.state,
                "relative": _relative_time(ts) if ts is not None
                else _compute_elapsed(lr.created_at),
            }
        wf_cards.append({
            "id": str(wf.id),
            "name": wf.name,
            "version": wf.version,
            "description": wf.description or "",
            "is_active": wf.is_active,
            "category_id": str(wf.workflow_category_id) if wf.workflow_category_id else "",
            "category_name": cat.name if cat else None,
            "steps": len(parsed.steps) if parsed else 0,
            "policy_count": policy_counts.get(wf.id, 0),
            "gate_count": gate_counts.get(wf.id, 0),
            "in_flight": in_flight.get(wf.id, 0),
            "last_run": last_run,
        })

    # Categories rail: only categories with at least one active workflow.
    cat_entries: list[dict] = []
    for cid, cat in cat_by_id.items():
        cat_workflows = [w for w in workflows if w.workflow_category_id == cid]
        if not cat_workflows:
            continue
        cat_entries.append({
            "id": str(cid),
            "name": cat.name,
            "description": cat.description or "",
            "workflow_count": len(cat_workflows),
            "in_flight": sum(in_flight.get(w.id, 0) for w in cat_workflows),
        })

    # Recent runs across the project (client filters by selected category).
    recent: list[dict] = []
    rr_result = await db.execute(
        select(Run)
        .options(selectinload(Run.workflow))
        .where(Run.project_id == project_id)
        .order_by(Run.created_at.desc())
        .limit(15)
    )
    recent_runs = rr_result.scalars().unique().all()
    max_ts: dict = {}
    if recent_runs:
        recent_ids = [r.id for r in recent_runs]
        mts_result = await db.execute(
            select(Transition.run_id, func.max(Transition.ts))
            .where(Transition.run_id.in_(recent_ids))
            .group_by(Transition.run_id)
        )
        max_ts = dict(mts_result.all())
    for run in recent_runs:
        updated_ts = float(max_ts[run.id]) if run.id in max_ts else None
        recent.append({
            "run_id": str(run.id),
            "workflow_id": str(run.workflow_id),
            "workflow_name": run.workflow.name if run.workflow else "",
            "story_id": run.story_id,
            "state": run.state,
            "needs_review": run.state == "paused",
            "updated": _relative_time(updated_ts) if updated_ts is not None
            else _compute_elapsed(run.created_at),
            "elapsed": _compute_elapsed(run.created_at),
        })

    return {
        "categories": cat_entries,
        "workflows": wf_cards,
        "recent_runs": recent,
    }


@router.patch("/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_session),
    pm: tuple[User, Identity] = Depends(require_project_manager),
) -> ProjectResponse:
    """Update a project's description (PM) or name (platform ADMIN only).

    ``require_project_manager`` admits platform ADMINs even without a
    membership row, so both roles reach the description path; the rename
    stays gated on ``platform_role == "ADMIN"`` explicitly.
    """
    current_user, _ = pm

    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")

    if body.name is not None:
        if current_user.platform_role != "ADMIN":
            raise HTTPException(403, "Only a platform ADMIN can rename a project")
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

    logger.info("Project updated: %s name=%s user=%s", project_id, project.name, current_user.id)
    return _to_response(project)


# ── Memberships (project-manager managed) ─────────────────────────────────────


def _member_to_response(membership, user_name: str = "", user_email: str = "") -> MemberResponse:
    return MemberResponse(
        id=str(membership.id),
        user_id=str(membership.user_id),
        user_name=user_name,
        user_email=user_email,
        role=membership.role,
        created_at=membership.created_at.isoformat() if membership.created_at else "",
    )


async def _pm_count(project_id: str, db: AsyncSession) -> int:
    from sqlalchemy import func

    return (await db.execute(
        select(func.count()).select_from(Membership).where(
            Membership.project_id == project_id,
            Membership.role == "project_manager",
        )
    )).scalar_one()


@router.get("/{project_id}/members")
async def list_members(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> list[MemberResponse]:
    """List all members of a project with user details (any project member)."""
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


@router.get("/{project_id}/members/candidates")
async def list_member_candidates(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> list[MemberCandidateResponse]:
    """Enabled users who are not yet members of this project (any project member)."""
    result = await db.execute(
        select(User)
        .outerjoin(
            Membership,
            (Membership.user_id == User.id) & (Membership.project_id == project_id),
        )
        .where(User.is_enabled == True, Membership.id.is_(None))
        .order_by(User.display_name)
    )
    return [
        MemberCandidateResponse(id=str(u.id), name=u.display_name, email=u.email)
        for u in result.scalars().all()
    ]


@router.post("/{project_id}/members", status_code=201)
async def add_member(
    project_id: str,
    body: MemberAdd,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> MemberResponse:
    """Add a user to the project. Requires ``project_manager`` role."""
    project = await db.get(Project, project_id)

    user = await db.get(User, body.user_id)
    if user is None:
        raise HTTPException(404, f"User {body.user_id} not found")

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
        "Member added by PM: user=%s project=%s role=%s",
        body.user_id, project_id, body.role,
    )
    return _member_to_response(membership, user.display_name, user.email)


@router.patch("/{project_id}/members/{membership_id}")
async def update_member_role(
    project_id: str,
    membership_id: str,
    body: MemberUpdate,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> MemberResponse:
    """Change a member's project role. Requires ``project_manager`` role."""
    membership = await db.get(Membership, membership_id)
    if membership is None or str(membership.project_id) != project_id:
        raise HTTPException(404, f"Membership {membership_id} not found in project {project_id}")

    # Guard: can't demote the last project_manager
    if (membership.role == "project_manager" and body.role != "project_manager"
            and await _pm_count(project_id, db) <= 1):
        raise HTTPException(400, "Cannot demote the last project manager")

    membership.role = body.role
    await db.commit()
    await db.refresh(membership)

    user = await db.get(User, membership.user_id)
    logger.info(
        "Member role updated by PM: membership=%s project=%s new_role=%s",
        membership_id, project_id, body.role,
    )
    return _member_to_response(
        membership,
        user_name=user.display_name if user else "",
        user_email=user.email if user else "",
    )


@router.delete("/{project_id}/members/{membership_id}")
async def remove_member(
    project_id: str,
    membership_id: str,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
):
    """Remove a member from the project. Requires ``project_manager`` role."""
    membership = await db.get(Membership, membership_id)
    if membership is None or str(membership.project_id) != project_id:
        raise HTTPException(404, f"Membership {membership_id} not found in project {project_id}")

    # Guard: can't remove the last project_manager
    if membership.role == "project_manager" and await _pm_count(project_id, db) <= 1:
        raise HTTPException(400, "Cannot remove the last project manager")

    await db.delete(membership)
    await db.commit()

    logger.info(
        "Member removed by PM: membership=%s user=%s project=%s",
        membership_id, membership.user_id, project_id,
    )
    return Response(status_code=204)
