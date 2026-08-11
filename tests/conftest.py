"""Shared test fixtures — available to all test layers."""

import pytest


@pytest.fixture
def app_config():
    """Return a test AppConfig with dev defaults."""
    from bheembhai.config import AppConfig
    return AppConfig.from_env()
