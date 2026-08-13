"""Crash recovery — re-enqueues stale claimed work items on Engine restart (ADR-003)."""

import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.config import AppConfig
from bheembhai.database import get_sessionmaker
from bheembhai.models.work_queue import WorkQueueItem

logger = logging.getLogger(__name__)


async def recover_on_startup(config: AppConfig) -> int:
    """Detect and re-enqueue stale claimed work items.

    On Engine restart, any items left in 'claimed' state with a stale heartbeat
    (> stale_heartbeat_threshold_seconds) are considered orphaned. They are
    reset to 'pending' so they can be re-claimed.

    Returns the count of recovered items.
    """
    sessionmaker = get_sessionmaker()
    if sessionmaker is None:
        logger.warning("Database not initialised — skipping crash recovery")
        return 0

    threshold = config.engine.stale_heartbeat_threshold_seconds
    stale_since = datetime.now(timezone.utc).timestamp() - threshold

    async with sessionmaker() as session:
        # Find items with stale heartbeats
        stmt = select(WorkQueueItem).where(
            WorkQueueItem.state == "claimed",
        )
        result = await session.execute(stmt)
        stale_items = [
            item for item in result.scalars().all()
            if item.heartbeat_at is not None
            and item.heartbeat_at.timestamp() < stale_since
        ]

        if not stale_items:
            logger.info("Crash recovery: no stale items found")
            return 0

        # Re-enqueue stale items
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

        await session.commit()
        logger.info("Crash recovery: re-enqueued %d stale items", len(stale_items))

        # TODO (BEEM-24): For each recovered run, check if the Fargate task
        # is still alive (via steps.fargate_task_arn). If alive, re-attach
        # rather than re-launch. If dead, clean up the task and restart the step.

        return len(stale_items)
