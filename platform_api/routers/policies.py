"""Policy endpoints for project managers (project-scoped workflows only).

Ownership flows through the policy's workflow: only a project_manager of the
workflow's project can create, update, or delete its policies. Platform
templates stay admin-managed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.models.user import User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select

from platform_api.dependencies import get_current_enabled_user
from platform_api.routers._workflow_shared import (
    _parse_policy_yaml,
    _policy_to_response,
    _require_pm_of_workflow,
)
from platform_api.schemas.admin import PolicyCreate, PolicyResponse, PolicyUpdate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/policies", tags=["policies"])


async def _get_policy_or_404(policy_id: str, db: AsyncSession) -> Policy:
    policy = await db.get(Policy, policy_id)
    if policy is None:
        raise HTTPException(404, f"Policy {policy_id} not found")
    return policy


@router.post("", status_code=201)
async def create_policy(
    body: PolicyCreate,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> PolicyResponse:
    """Create a new policy tied to a project-scoped workflow (PM of that project)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, body.workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {body.workflow_id} not found")
    await _require_pm_of_workflow(workflow, current_user, db)

    # Check for duplicate — uniqueness on (workflow_id, name, version)
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
        project_id=workflow.project_id,  # inherit the workflow's project
        workflow_id=body.workflow_id,
        name=body.name.strip(),
        version=version,
        yaml_content=body.yaml_content,
        is_active=True,
    )
    db.add(policy)
    await db.commit()

    logger.info("Policy created by PM: %s name=%s workflow=%s", policy.id, policy.name, body.workflow_id)
    return _policy_to_response(policy, workflow.name)


@router.patch("/{policy_id}")
async def update_policy(
    policy_id: str,
    body: PolicyUpdate,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> PolicyResponse:
    """Update a policy's YAML content or active status (PM of its workflow's project)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    policy = await _get_policy_or_404(policy_id, db)
    workflow = await db.get(Workflow, policy.workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {policy.workflow_id} not found")
    await _require_pm_of_workflow(workflow, current_user, db)

    if body.yaml_content is not None:
        policy.yaml_content = body.yaml_content
        parsed = _parse_policy_yaml(body.yaml_content)
        if parsed:
            policy.version = parsed.version
    if body.is_active is not None:
        policy.is_active = body.is_active

    await db.commit()

    logger.info("Policy updated by PM: %s", policy_id)
    return _policy_to_response(policy, workflow.name)


@router.delete("/{policy_id}")
async def delete_policy(
    policy_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
):
    """Delete a policy (PM of its workflow's project)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    policy = await _get_policy_or_404(policy_id, db)
    workflow = await db.get(Workflow, policy.workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {policy.workflow_id} not found")
    await _require_pm_of_workflow(workflow, current_user, db)

    await db.delete(policy)
    await db.commit()

    logger.info("Policy deleted by PM: %s name=%s", policy_id, policy.name)
    return Response(status_code=204)
