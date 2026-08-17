"""Engine event receiver — the platform side of the engine→platform push channel.

The UI polls the DB, so these events are NOT correctness-critical (losing one
costs a poll interval). The endpoint is deliberately thin: verify the shared
webhook secret, log the event, ack with 202. No state is written here — the
DB is the single source of truth and the engine already committed by the time
this fires.
"""

import logging
import os

from fastapi import APIRouter, Header, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/engine", status_code=202)
async def engine_webhook(
    request: Request,
    x_bb_secret: str | None = Header(default=None),
) -> dict:
    """Receive an engine push event (step progress, approval_required, …).

    Auth is the shared ``BB_WEBHOOK_SECRET`` in ``X-BB-Secret``. Under
    ``DEV_AUTH_BYPASS`` the secret check is skipped — local dev doesn't
    coordinate secrets across services.
    """
    config = request.app.state.config
    expected = config.engine.webhook_secret
    if os.getenv("DEV_AUTH_BYPASS", "").lower() != "true" and x_bb_secret != expected:
        raise HTTPException(401, "Invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        body = {}
    event = body.get("event") if isinstance(body, dict) else None
    logger.info("engine webhook: %s (run %s, step %s)",
                (event or {}).get("type", "unknown"),
                (event or {}).get("run_id", "?"),
                (event or {}).get("step_id", "-"))
    return {"received": True}
