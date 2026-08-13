"""Workflow and policy endpoints for users.

Members can list workflows/policies (used by the new-run form). Project
managers additionally manage their project's workflows: copy platform
templates, edit, deactivate, delete.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import or_, select

from bheembhai.database import get_session
from bheembhai.models.project import Project
from bheembhai.models.run import Run
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_enabled_user
from platform_api.routers._workflow_shared import (
    _parse_workflow_yaml,
    _policy_to_response,
    _require_pm_of_workflow,
    _workflow_to_response,
)
from platform_api.schemas.admin import (
    CopyToProjectRequest,
    PolicyResponse,
    WorkflowResponse,
    WorkflowUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_workflows(
    include_inactive: bool = Query(False),
    project_id: str | None = Query(None),
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[WorkflowResponse]:
    """List workflows.

    - ``project_id=__platform__`` → platform templates only (any authenticated user)
    - ``project_id=<uuid>`` → project-scoped + platform templates (members);
      with ``include_inactive=true`` → project-scoped only incl. inactive (PM)
    - no ``project_id`` → platform templates only
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    is_real_project = bool(project_id and project_id != "__platform__")

    if is_real_project:
        # Verify membership
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
        if include_inactive and membership.role != "project_manager":
            raise HTTPException(403, "Only a project manager can do this")

    stmt = select(Workflow).order_by(Workflow.created_at.desc())

    if project_id == "__platform__":
        stmt = stmt.where(Workflow.project_id.is_(None))
    elif is_real_project:
        if include_inactive:
            # PM management view: only this project's workflows
            stmt = stmt.where(Workflow.project_id == project_id)
        else:
            # Member view (new-run form): project-scoped + platform templates
            stmt = stmt.where(
                or_(Workflow.project_id == project_id, Workflow.project_id.is_(None))
            )
    else:
        stmt = stmt.where(Workflow.project_id.is_(None))

    if not (include_inactive and is_real_project):
        stmt = stmt.where(Workflow.is_active == True)

    result = await db.execute(stmt)
    return [await _workflow_to_response(w, db) for w in result.scalars().all()]


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Get a workflow by ID (any authenticated user)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    return await _workflow_to_response(workflow, db)


@router.get("/{workflow_id}/policies")
async def list_policies(
    workflow_id: str,
    include_inactive: bool = Query(False),
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[PolicyResponse]:
    """List policies for a workflow.

    Default: active policies only (any authenticated user — used by the
    new-run form). ``include_inactive=true`` additionally requires the
    ``project_manager`` role on the workflow's project.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    if include_inactive:
        await _require_pm_of_workflow(workflow, current_user, db)

    stmt = (
        select(Policy)
        .where(Policy.workflow_id == workflow_id)
        .order_by(Policy.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(Policy.is_active == True)

    result = await db.execute(stmt)
    return [_policy_to_response(p, workflow.name) for p in result.scalars().all()]


@router.post("/{workflow_id}/copy-to-project", status_code=201)
async def copy_workflow_to_project(
    workflow_id: str,
    body: CopyToProjectRequest,
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Clone a platform workflow (and its policies) to a project.

    Requires the ``project_manager`` role in the target project. The source
    workflow remains as a platform template; the clone gets ``project_id``
    set so the project can customise it independently.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    source = await db.get(Workflow, workflow_id)
    if source is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")
    if source.project_id is not None:
        raise HTTPException(403, "Only platform templates can be copied to a project")

    # PM check on the target project
    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == current_user.id,
                Membership.project_id == body.project_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.role != "project_manager":
        raise HTTPException(403, "Only a project manager can do this")

    project = await db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, f"Project {body.project_id} not found")

    # Check for duplicate (project_id, name, version) — project-scoped uniqueness
    existing = (
        await db.execute(
            select(Workflow).where(
                Workflow.project_id == body.project_id,
                Workflow.name == source.name,
                Workflow.version == source.version,
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
        "Workflow cloned by PM: %s → %s (project=%s)",
        source.id, clone.id, body.project_id,
    )
    return await _workflow_to_response(clone, db)


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdate,
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Update a project-scoped workflow's name, YAML, or active status.

    Requires the ``project_manager`` role in the workflow's project.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    await _require_pm_of_workflow(workflow, current_user, db)

    if body.name is not None:
        name = body.name.strip()
        # Check for duplicate (excluding self) — uniqueness within the project
        existing = (
            await db.execute(
                select(Workflow).where(
                    Workflow.project_id == workflow.project_id,
                    Workflow.name == name,
                    Workflow.version == workflow.version,
                    Workflow.id != workflow.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                409,
                f"A workflow named '{name}' version {workflow.version} already exists in this project",
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

    logger.info("Workflow updated by PM: %s", workflow_id)
    return await _workflow_to_response(workflow, db)


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    db: "AsyncSession" = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
):
    """Delete a project-scoped workflow and all associated policies and runs.

    Children are deleted explicitly because the FK columns
    (``runs.workflow_id``, ``runs.policy_id``, ``policies.workflow_id``)
    lack ``ON DELETE CASCADE`` at the database level.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    await _require_pm_of_workflow(workflow, current_user, db)

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

    logger.info("Workflow deleted by PM: %s name=%s", workflow_id, workflow.name)
    return Response(status_code=204)
