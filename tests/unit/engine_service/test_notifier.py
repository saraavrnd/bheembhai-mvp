"""Unit tests — engine→platform notifier (ADR-013 §6 fire-and-forget).

Covers: no-op when unconfigured, delivery with the shared secret header,
retry-once-then-drop, and drop-on-full. Uses httpx.MockTransport so no real
platform is involved; the module-global queue is reset around each test.
"""

import asyncio
import json
import logging

import httpx
import pytest

from engine_service import notifier


class _EngineCfg:
    def __init__(self, url: str = "http://platform", secret: str = "sekret"):
        self.platform_api_url = url
        self.webhook_secret = secret


class _Cfg:
    def __init__(self, url: str = "http://platform"):
        self.engine = _EngineCfg(url=url)


@pytest.fixture(autouse=True)
def _reset_notifier():
    """The queue is module-global — isolate tests from each other."""
    notifier._QUEUE = None
    yield
    notifier._QUEUE = None


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_setup_without_platform_url_is_noop():
    cfg = _Cfg(url="")
    assert notifier.setup_notifier(cfg) is None
    assert notifier._QUEUE is None


async def test_publish_without_setup_is_noop():
    await notifier.publish({"type": "step_completed"})  # must not raise


async def test_event_delivered_with_secret_header():
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(202, json={"received": True})

    task = notifier.setup_notifier(_Cfg(), transport=httpx.MockTransport(handler))
    assert task is not None
    try:
        await notifier.publish({"type": "approval_required", "run_id": "r1", "step_id": "code-review"})
        assert await _wait_for(lambda: len(sent) == 1)
        req = sent[0]
        assert str(req.url).endswith("/webhooks/engine")
        assert req.headers["X-BB-Secret"] == "sekret"
        assert json.loads(req.content)["event"]["type"] == "approval_required"
    finally:
        await _cancel(task)


async def test_retry_once_then_drop(caplog):
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("platform down")

    with caplog.at_level(logging.WARNING):
        task = notifier.setup_notifier(_Cfg(), transport=httpx.MockTransport(handler))
        try:
            await notifier.publish({"type": "step_completed"})
            assert await _wait_for(lambda: len(attempts) == 2)
            # exactly two attempts — no third delivery after the drop
            assert await _wait_for(lambda: "dropped step_completed event" in caplog.text)
            await asyncio.sleep(0.1)
            assert len(attempts) == 2
            assert notifier._QUEUE.empty()
        finally:
            await _cancel(task)


async def test_http_error_then_success_on_retry(caplog):
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(500)
        return httpx.Response(202, json={"received": True})

    with caplog.at_level(logging.WARNING):
        task = notifier.setup_notifier(_Cfg(), transport=httpx.MockTransport(handler))
        try:
            await notifier.publish({"type": "step_completed"})
            assert await _wait_for(lambda: len(attempts) == 2)
            assert "dropped" not in caplog.text  # second attempt succeeded
            assert notifier._QUEUE.empty()
        finally:
            await _cancel(task)


async def test_queue_full_drops_event():
    # Small queue with no consumer — put_nowait fills it, publish drops quietly.
    notifier._QUEUE = asyncio.Queue(maxsize=2)
    notifier._QUEUE.put_nowait({"type": "a"})
    notifier._QUEUE.put_nowait({"type": "b"})
    await notifier.publish({"type": "c"})  # must not raise
    assert notifier._QUEUE.qsize() == 2
