"""Configuration loader — reads env vars and optional YAML config."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DatabaseConfig:
    url: str = "postgresql+asyncpg://bheembhai:bheembhai@localhost:5432/bheembhai"
    echo: bool = False

    @classmethod
    def from_env(cls) -> "DatabaseConfig":
        return cls(
            url=os.getenv("DATABASE_URL", cls.url),
            echo=os.getenv("DB_ECHO", "").lower() == "true",
        )


@dataclass
class AuthConfig:
    provider: str = "cognito"  # "cognito", "azure_ad", "okta"
    cognito_region: str = "us-east-1"
    cognito_user_pool_id: str = ""
    cognito_client_id: str = ""
    azure_ad_tenant_id: str = ""
    azure_ad_client_id: str = ""
    okta_domain: str = ""
    okta_client_id: str = ""

    @classmethod
    def from_env(cls) -> "AuthConfig":
        return cls(
            provider=os.getenv("AUTH_PROVIDER", "cognito"),
            cognito_region=os.getenv("COGNITO_REGION", "us-east-1"),
            cognito_user_pool_id=os.getenv("COGNITO_USER_POOL_ID", ""),
            cognito_client_id=os.getenv("COGNITO_CLIENT_ID", ""),
            azure_ad_tenant_id=os.getenv("AZURE_AD_TENANT_ID", ""),
            azure_ad_client_id=os.getenv("AZURE_AD_CLIENT_ID", ""),
            okta_domain=os.getenv("OKTA_DOMAIN", ""),
            okta_client_id=os.getenv("OKTA_CLIENT_ID", ""),
        )


@dataclass
class StorageConfig:
    backend: str = "local"  # "s3", "azure_blob", "minio", "local"
    s3_bucket: str = "bheembhai-artifacts"
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""  # for MinIO / LocalStack
    azure_blob_connection_string: str = ""
    azure_blob_container: str = "bheembhai-artifacts"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "bheembhai-artifacts"
    local_base_path: str = "/tmp/bheembhai-artifacts"

    @classmethod
    def from_env(cls) -> "StorageConfig":
        return cls(
            backend=os.getenv("STORAGE_BACKEND", "local"),
            s3_bucket=os.getenv("S3_BUCKET", "bheembhai-artifacts"),
            s3_region=os.getenv("S3_REGION", "us-east-1"),
            s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
            azure_blob_connection_string=os.getenv("AZURE_BLOB_CONNECTION_STRING", ""),
            azure_blob_container=os.getenv("AZURE_BLOB_CONTAINER", "bheembhai-artifacts"),
            minio_endpoint=os.getenv("MINIO_ENDPOINT", "localhost:9000"),
            minio_access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
            minio_bucket=os.getenv("MINIO_BUCKET", "bheembhai-artifacts"),
            local_base_path=os.getenv("LOCAL_STORAGE_PATH", "/tmp/bheembhai-artifacts"),
        )


@dataclass
class SecureStorageConfig:
    backend: str = "env"  # "aws_ssm", "aws_secrets_manager", "azure_key_vault", "hashicorp_vault", "env"
    aws_region: str = "us-east-1"
    azure_vault_url: str = ""
    vault_url: str = ""
    vault_token_env: str = "VAULT_TOKEN"
    env_encrypted_config_path: str = ""

    @classmethod
    def from_env(cls) -> "SecureStorageConfig":
        return cls(
            backend=os.getenv("SECURE_STORAGE_BACKEND", "env"),
            aws_region=os.getenv("AWS_REGION", "us-east-1"),
            azure_vault_url=os.getenv("AZURE_VAULT_URL", ""),
            vault_url=os.getenv("VAULT_URL", ""),
            vault_token_env=os.getenv("VAULT_TOKEN_ENV", "VAULT_TOKEN"),
            env_encrypted_config_path=os.getenv("ENCRYPTED_CONFIG_PATH", ""),
        )


@dataclass
class EngineConfig:
    engine_id: str = "engine-1"
    heartbeat_interval_seconds: int = 30
    stale_heartbeat_threshold_seconds: int = 60
    poll_interval_seconds: int = 5

    @classmethod
    def from_env(cls) -> "EngineConfig":
        return cls(
            engine_id=os.getenv("ENGINE_ID", "engine-1"),
            heartbeat_interval_seconds=int(os.getenv("BB_HEARTBEAT_INTERVAL", "30")),
            stale_heartbeat_threshold_seconds=int(os.getenv("BB_STALE_HEARTBEAT_THRESHOLD", "60")),
            poll_interval_seconds=int(os.getenv("BB_POLL_INTERVAL", "5")),
        )


@dataclass
class AppConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    secure_storage: SecureStorageConfig = field(default_factory=SecureStorageConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    git_remote_url: str = ""
    git_source_branch: str = "main"
    secret_key: str = "change-me-in-production"

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            database=DatabaseConfig.from_env(),
            auth=AuthConfig.from_env(),
            storage=StorageConfig.from_env(),
            secure_storage=SecureStorageConfig.from_env(),
            engine=EngineConfig.from_env(),
            git_remote_url=os.getenv("GIT_REMOTE_URL", ""),
            git_source_branch=os.getenv("GIT_SOURCE_BRANCH", "main"),
            secret_key=os.getenv("SECRET_KEY", "change-me-in-production"),
        )


def load_config(config_path: str | None = None) -> AppConfig:
    """Load config from env vars, optionally overlaid with a YAML file."""
    config = AppConfig.from_env()

    if config_path:
        path = Path(config_path)
        if path.exists():
            with open(path) as f:
                file_config: dict[str, Any] = yaml.safe_load(f) or {}
            _apply_yaml_overlay(config, file_config)

    return config


def _apply_yaml_overlay(config: AppConfig, overlay: dict[str, Any]) -> None:
    """Apply YAML config values on top of env-derived defaults."""
    for section_name, section_values in overlay.items():
        if hasattr(config, section_name):
            section = getattr(config, section_name)
            if isinstance(section_values, dict):
                for key, value in section_values.items():
                    if hasattr(section, key):
                        setattr(section, key, value)
