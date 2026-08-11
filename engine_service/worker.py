"""Worker loop — claims work from work_queue via SKIP LOCKED (ADR-003)."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.config import AppConfig
from bheembhai.database import _sessionmaker
from bheembhai.models.work_queue import WorkQueueItem

logger = logging.getLogger(__name__)


async def worker_loop(config: AppConfig) -> None:
    """Continuously poll work_queue for pending items, claim and process them.

    Runs as a background asyncio task for the lifetime of the Engine service.
    Each Engine instance independently polls; SKIP LOCKED ensures no two engines
    claim the same work item.

    A companion heartbeat task runs in parallel to keep claimed items alive.
    """
    heartbeat_task = asyncio.create_task(_heartbeat_loop(config))

    try:
        while True:
            if _sessionmaker is None:
                await asyncio.sleep(1)
                continue

            try:
                async with _sessionmaker() as session:
                    claimed = await _claim_next_item(session, config)
                    if claimed:
                        await _process_item(session, claimed, config)
            except Exception:
                logger.exception("Worker loop iteration failed — retrying")

            await asyncio.sleep(config.engine.poll_interval_seconds)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _claim_next_item(
    session: AsyncSession, config: AppConfig
) -> WorkQueueItem | None:
    """Claim the oldest pending work item using SELECT ... FOR UPDATE SKIP LOCKED."""
    stmt = (
        select(WorkQueueItem)
        .where(WorkQueueItem.state == "pending")
        .order_by(WorkQueueItem.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(stmt)
    item = result.scalar_one_or_none()

    if item is None:
        return None

    now = datetime.now(timezone.utc)
    item.state = "claimed"
    item.claimed_by = config.engine.engine_id
    item.claimed_at = now
    item.heartbeat_at = now
    await session.commit()
    await session.refresh(item)

    logger.info(
        "Claimed work item id=%s run_id=%s action=%s",
        item.id, item.run_id, item.action,
    )
    return item


async def _process_item(
    session: AsyncSession, item: WorkQueueItem, config: AppConfig
) -> None:
    """Process a claimed work item.

    For MVP: this is a skeleton. The real implementation will:
      - For action='start': launch a Fargate task, monitor it, handle completion.
      - For action='continue': resume the paused state machine after gate approval.
    """
    logger.info(
        "Processing work item id=%s action=%s payload=%s",
        item.id, item.action, item.payload,
    )

    # TODO: Real state machine execution here (BEEM-24 implement phase)
    # For the walking skeleton, just mark it done immediately.
    item.state = "done"
    await session.commit()


async def _heartbeat_loop(config: AppConfig) -> None:
    """Periodically update heartbeat_at for all items this engine has claimed."""
    while True:
        await asyncio.sleep(config.engine.heartbeat_interval_seconds)
        if _sessionmaker is None:
            continue
        try:
            async with _sessionmaker() as session:
                now = datetime.now(timezone.utc)
                stmt = (
                    update(WorkQueueItem)
                    .where(
                        WorkQueueItem.state == "claimed",
                        WorkQueueItem.claimed_by == config.engine.engine_id,
                    )
                    .values(heartbeat_at=now)
                )
                await session.execute(stmt)
                await session.commit()
        except Exception:
            logger.exception("Heartbeat update failed")
