"""Engine → Platform API webhook endpoints (stubs)."""

from fastapi import APIRouter

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/engine")
async def engine_webhook() -> dict:
    """Receive engine state updates (step complete, run finished, etc.)."""
    return {"received": True}
