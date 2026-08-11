"""Integration test — full app health check (walking skeleton)."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Full app with lifespan — integration test."""
    from platform_api.main import app
    return TestClient(app)


def test_health_integration(client):
    """The full app boots and /health returns ok."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
