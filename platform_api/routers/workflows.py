"""Workflow and policy CRUD endpoints."""

from fastapi import APIRouter, Depends

from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_user

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("")
async def list_workflows(
    user: Identity | None = Depends(get_current_user),
) -> list:
    """List workflows for a project."""
    return []


@router.post("")
async def create_workflow(
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Create a new workflow version."""
    return {"id": "stub"}


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Get a workflow by ID."""
    return {"id": workflow_id}


@router.post("/{workflow_id}/policies")
async def create_policy(
    workflow_id: str,
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Create a policy tied to this workflow."""
    return {"id": "stub", "workflow_id": workflow_id}
