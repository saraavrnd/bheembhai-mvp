"""Engine health endpoint — reports liveness, queue depth, and dispatch activity."""

from fastapi import APIRouter, Request

from engine_service.metrics import METRICS

router = APIRouter(tags=["health"])


@router.get("/engine/health")
async def health(request: Request) -> dict:
    config = request.app.state.config
    return {
        "status": "ok",
        "service": "engine-service",
        "engine_id": config.engine.engine_id,
        "orphaned_items": METRICS.orphaned_items,      # from crash recovery
        "queue_depth": METRICS.queue_depth,            # from the worker claim pass
        "active_dispatches": METRICS.active_dispatches,
    }
