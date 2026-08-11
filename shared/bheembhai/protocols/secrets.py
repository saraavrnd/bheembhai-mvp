"""Pluggable secure storage protocol (ADR-012)."""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Credential:
    """A retrieved credential — raw value is never logged or persisted."""
    ref: str       # Opaque, provider-specific reference (ARN, vault path, key name)
    value: str     # The retrieved secret value
    provider: str  # "aws_ssm", "aws_secrets_manager", "azure_key_vault", "hashicorp_vault", "env"


class SecureStorage(Protocol):
    """Pluggable credential storage. One implementation per deployment."""

    backend_name: str

    async def get(self, ref: str) -> Credential | None:
        """Retrieve a secret by its provider-specific reference. None if not found."""
        ...

    async def put(self, ref: str, value: str, metadata: dict | None = None) -> str:
        """Store a secret. Returns the provider-specific reference (e.g., ARN)."""
        ...

    async def delete(self, ref: str) -> None:
        """Delete a secret. No-op if not found."""
        ...
