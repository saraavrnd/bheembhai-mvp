"""Unit test — Platform API health endpoint (walking skeleton)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from platform_api.main import app
    return TestClient(app)


def test_health_endpoint_returns_ok(client):
    """GET /health returns 200 and status ok."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "platform-api"
