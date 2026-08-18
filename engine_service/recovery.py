"""Crash recovery — heals orphaned work on Engine restart (ADR-003).

Step 1: stale `claimed` items (heartbeat older than the threshold) are reset to
`pending` for re-claim.

Step 2: every in-flight run (`running`/`paused`) must have exactly one
pending/claimed dispatch token. A crash between "run state committed" and
"work item committed" (or a crash that lost the item) leaves a run that will
never move again — enqueue a `continue {action: resume}` token. The dispatch
is idempotent: the state machine resumes from persisted state, and a paused
run's resume just re-notifies the open gate.
"""

import logging
from datetime import datetime, timezone

from bheembhai.config import AppConfig
from bheembhai.database import get_sessionmaker
from bheembhai.models.run import Run
from bheembhai.models.work_queue import WorkQueueItem
from sqlalchemy import select

from engine_service.metrics import METRICS

logger = logging.getLogger(__name__)


async def recover_on_startup(config: AppConfig) -> int:
    """Detect and re-enqueue stale claimed work items, then top up dispatch
    tokens for in-flight runs.

    Returns the count of recovered items.
    """
    sessionmaker = get_sessionmaker()
    if sessionmaker is None:
        logger.warning("Database not initialised — skipping crash recovery")
        return 0

    threshold = config.engine.stale_heartbeat_threshold_seconds
    stale_since = datetime.now(timezone.utc).timestamp() - threshold

    async with sessionmaker() as session:
        # ── Step 1: re-enqueue stale claimed items ──
        result = await session.execute(
            select(WorkQueueItem).where(WorkQueueItem.state == "claimed"))
        stale_items = [
            item for item in result.scalars().all()
            if item.heartbeat_at is not None
            and item.heartbeat_at.timestamp() < stale_since
        ]

        if not stale_items:
            logger.info("Crash recovery: no stale items found")
        for item in stale_items:
            logger.warning(
                "Recovering stale work item id=%s run_id=%s (was claimed by %s, "
                "heartbeat_at=%s)",
                item.id, item.run_id, item.claimed_by, item.heartbeat_at,
            )
            item.state = "pending"
            item.claimed_by = None
            item.claimed_at = None
            item.heartbeat_at = None

        METRICS.orphaned_items = len(stale_items)

        # ── Step 2: in-flight runs need exactly one dispatch token ──
        result = await session.execute(
            select(Run).where(Run.state.in_(("running", "paused"))))
        enqueued = 0
        for run in result.scalars().all():
            has_token = await session.execute(
                select(WorkQueueItem.id)
                .where(WorkQueueItem.run_id == run.id,
                       WorkQueueItem.state.in_(("pending", "claimed")))
                .limit(1))
            if has_token.first() is None:
                session.add(WorkQueueItem(
                    run_id=run.id, action="continue", payload={"action": "resume"}))
                enqueued += 1
                logger.warning("Recovery: no dispatch token for in-flight run %s (%s) — "
                               "enqueued resume", run.id, run.state)

        await session.commit()
        logger.info("Crash recovery: re-enqueued %d stale items, %d resume tokens",
                    len(stale_items), enqueued)

        # A stale item's dispatch will resume its step from persisted state
        # (exec_state + fargate_task_arn): the container is re-attached if it
        # survived, relaunched at the same attempt otherwise.

        return len(stale_items)
