"""Trivial unit test — proves config loading works (walking skeleton)."""

from bheembhai.config import AppConfig


def test_config_defaults():
    """AppConfig loads sensible defaults from environment."""
    config = AppConfig.from_env()
    assert config.database.url is not None
    assert config.auth.provider == "cognito"
    assert config.storage.backend == "local"
    assert config.secure_storage.backend == "env"
    assert config.engine.engine_id == "engine-1"
    assert config.engine.heartbeat_interval_seconds == 30
