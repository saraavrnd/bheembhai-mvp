"""Unit test — Engine Service health endpoint (walking skeleton)."""

import pytest
from fastapi import FastAPI

from engine_service.routers.health import router as health_router


@pytest.fixture
def client():
    """Create a minimal app with just the health router — no lifespan."""
    app = FastAPI()
    app.state.config = type("cfg", (), {"engine": type("e", (), {"engine_id": "test-engine"})()})()
    app.include_router(health_router)
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_engine_health_endpoint_returns_ok(client):
    """GET /engine/health returns 200 and reports service name."""
    response = client.get("/engine/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "engine-service"
    assert data["engine_id"] == "test-engine"
