"""E2E walking skeleton — end-to-end smoke test via HTTP.

Uses httpx (already a shared dependency) to hit both services.
No browser automation required — keeps the dev-setup footprint small.
"""

import pytest
import httpx


PLATFORM_API = "http://localhost:9000"
ENGINE_SERVICE = "http://localhost:9001"


@pytest.mark.e2e
async def test_platform_health_via_http():
    """The platform API /health endpoint responds 200."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{PLATFORM_API}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "platform-api"


@pytest.mark.e2e
async def test_engine_health_via_http():
    """The engine service /engine/health endpoint responds 200."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ENGINE_SERVICE}/engine/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "engine-service"
