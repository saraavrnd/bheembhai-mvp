"""Unit tests — platform POST /webhooks/engine receiver.

The endpoint is deliberately thin: verify the shared BB_WEBHOOK_SECRET (skipped
under DEV_AUTH_BYPASS), parse the event for logging, ack with 202. No DB.
"""

import httpx
from fastapi import FastAPI

from platform_api.routers.webhooks import router


class _EngineCfg:
    webhook_secret = "sekret"


class _Cfg:
    engine = _EngineCfg()


def _app() -> FastAPI:
    app = FastAPI()
    app.state.config = _Cfg()
    app.include_router(router)
    return app


async def _post(client: httpx.AsyncClient, *, secret: str | None = "sekret", **kwargs):
    headers = {} if secret is None else {"X-BB-Secret": secret}
    return await client.post("/webhooks/engine", headers=headers, **kwargs)


async def test_valid_secret_accepted():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, json={"event": {"type": "step_completed", "run_id": "r1"}})
    assert resp.status_code == 202
    assert resp.json() == {"received": True}


async def test_wrong_secret_rejected():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, secret="nope", json={"event": {"type": "x"}})
    assert resp.status_code == 401


async def test_missing_secret_rejected():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, secret=None, json={"event": {"type": "x"}})
    assert resp.status_code == 401


async def test_dev_auth_bypass_skips_secret(monkeypatch):
    monkeypatch.setenv("DEV_AUTH_BYPASS", "true")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, secret=None, json={"event": {"type": "x"}})
    assert resp.status_code == 202


async def test_malformed_body_still_acked():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        resp = await _post(client, content=b"not json")
    assert resp.status_code == 202
    assert resp.json() == {"received": True}
