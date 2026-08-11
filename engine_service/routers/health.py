"""Engine health endpoint — reports liveness and queue depth."""

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/engine/health")
async def health(request: Request) -> dict:
    config = request.app.state.config
    return {
        "status": "ok",
        "service": "engine-service",
        "engine_id": config.engine.engine_id,
        "orphaned_items": None,  # populated by recovery module
        "queue_depth": None,      # populated by worker loop
    }
