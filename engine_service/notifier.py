"""Engine → Platform push notifications (ADR-013 §6 — fire-and-forget).

The platform UI polls the DB, so these events are NOT correctness-critical:
losing one only costs a poll interval before the UI catches up. That licence
shapes the design — a bounded in-process queue fed by the state machine's
publish hook, drained by ONE consumer task that POSTs to the platform's
`/webhooks/engine` receiver with the shared secret, one retry, then drop.

The publish callback is what the worker wires into `drive_run(publish=…)`;
when the notifier is not set up (no PLATFORM_API_URL) it is a safe no-op.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

_QUEUE: asyncio.Queue[dict] | None = None
_MAX_PENDING = 512
_RETRY_DELAY_S = 1.0

Publish = Callable[[dict], Awaitable[None]]


def setup_notifier(config, *, transport: httpx.AsyncBaseTransport | None = None) -> asyncio.Task | None:
    """Start the event consumer. Returns the task to cancel at shutdown, or None
    when the platform URL is unset (notifications disabled).

    `transport` exists for tests (httpx.MockTransport) — production uses the
    default client transport.
    """
    global _QUEUE
    url = (config.engine.platform_api_url or "").strip().rstrip("/")
    if not url:
        logger.info("PLATFORM_API_URL not set — engine→platform notifications disabled")
        return None
    _QUEUE = asyncio.Queue(maxsize=_MAX_PENDING)
    task = asyncio.create_task(_consumer(url, config.engine.webhook_secret, transport))
    logger.info("Notifier started → %s/webhooks/engine (queue cap %d)", url, _MAX_PENDING)
    return task


async def publish(event: dict) -> None:
    """The engine's publish hook — enqueue for delivery, drop when the queue is
    full or the notifier was never set up. Never raises into the state machine."""
    if _QUEUE is None:
        return
    try:
        _QUEUE.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("notifier queue full (%d) — dropping %s event",
                       _MAX_PENDING, event.get("type"))


async def _consumer(url: str, secret: str,
                    transport: httpx.AsyncBaseTransport | None) -> None:
    """Drain the queue forever: POST each event, retry once on failure, drop on
    the second failure. Cancelled by the caller at shutdown."""
    client = httpx.AsyncClient(timeout=5.0, transport=transport) if transport else httpx.AsyncClient(timeout=5.0)
    assert _QUEUE is not None
    try:
        while True:
            event = await _QUEUE.get()
            delivered = False
            for attempt in (1, 2):
                try:
                    resp = await client.post(
                        f"{url}/webhooks/engine",
                        json={"event": event},
                        headers={"X-BB-Secret": secret},
                    )
                    if resp.status_code < 400:
                        delivered = True
                        break
                    logger.warning("notifier: platform returned HTTP %d for %s event (attempt %d)",
                                   resp.status_code, event.get("type"), attempt)
                except httpx.HTTPError:
                    logger.warning("notifier: platform unreachable for %s event (attempt %d) — %s",
                                   event.get("type"), attempt, url)
                if attempt == 1:
                    await asyncio.sleep(_RETRY_DELAY_S)
            if not delivered:
                logger.warning("notifier: dropped %s event after retry", event.get("type"))
    finally:
        await client.aclose()
