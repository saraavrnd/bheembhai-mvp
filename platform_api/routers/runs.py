"""Run endpoints."""

from fastapi import APIRouter, Depends

from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_user

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("")
async def list_runs(
    user: Identity | None = Depends(get_current_user),
) -> list:
    """List runs for a project, newest first."""
    return []


@router.post("")
async def create_run(
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Start a new run. Writes to work_queue (ADR-003)."""
    return {"id": "stub"}


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Get run state including steps and transitions."""
    return {"id": run_id}


@router.post("/{run_id}/decision")
async def submit_decision(
    run_id: str,
    user: Identity | None = Depends(get_current_user),
) -> dict:
    """Approve or request changes at a gate."""
    return {"id": run_id, "decision": "approved"}
