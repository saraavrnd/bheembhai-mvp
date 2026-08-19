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


def test_engine_config_runtime_defaults():
    """ADR-013 engine knobs have dev-friendly defaults."""
    config = AppConfig.from_env()
    e = config.engine
    assert e.runtime == "docker"
    assert e.agent_image == "bheembhai/agent:latest"
    assert e.max_step_visits == 3
    assert e.max_attempts == 2
    assert e.gate_poll_interval_seconds == 5
    assert e.platform_api_url == "http://platform-api:8000"
    assert e.webhook_secret == "dev-secret"
    assert e.seed_on_startup is False
    assert "BB_MOCK" in e.env_forward


def test_engine_config_env_overrides(monkeypatch):
    """Every engine knob is env-drivable — no code edits to tune a deployment."""
    monkeypatch.setenv("BB_RUNTIME", "fargate")
    monkeypatch.setenv("BB_AGENT_IMAGE", "acme/agent:7")
    monkeypatch.setenv("DOCKER_ENDPOINT", "tcp://dockerd:2375")
    monkeypatch.setenv("BB_MAX_STEP_VISITS", "5")
    monkeypatch.setenv("BB_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("BB_KEEP_CONTAINERS", "1")
    monkeypatch.setenv("BB_GATE_POLL_INTERVAL", "9")
    monkeypatch.setenv("PLATFORM_API_URL", "http://platform:9000")
    monkeypatch.setenv("BB_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("BB_SEED_ON_STARTUP", "false")
    monkeypatch.setenv("BB_ENV_FORWARD", "FOO, BAR,")

    e = AppConfig.from_env().engine
    assert e.runtime == "fargate"
    assert e.agent_image == "acme/agent:7"
    assert e.docker_endpoint == "tcp://dockerd:2375"
    assert e.max_step_visits == 5
    assert e.max_attempts == 3
    assert e.keep_containers is True
    assert e.gate_poll_interval_seconds == 9
    assert e.platform_api_url == "http://platform:9000"
    assert e.webhook_secret == "s3cret"
    assert e.seed_on_startup is False
    assert e.env_forward == ["FOO", "BAR"]
