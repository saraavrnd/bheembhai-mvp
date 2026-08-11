"""Pluggable object storage protocol (ADR-011)."""

from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class StoredObject:
    """An object retrieved from storage."""
    key: str
    data: bytes
    content_type: str | None = None
    metadata: dict[str, str] | None = None


@dataclass
class PresignedUrl:
    """A time-limited pre-signed URL for direct access."""
    url: str
    expires_at: float  # Unix timestamp


class ObjectStorage(Protocol):
    """Pluggable artifact storage. One implementation per deployment."""

    backend_name: str

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Store an object at the given key."""
        ...

    async def get(self, key: str) -> StoredObject | None:
        """Retrieve an object by key. None if not found."""
        ...

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> PresignedUrl:
        """Generate a time-limited pre-signed download URL."""
        ...

    async def list(self, prefix: str) -> AsyncIterator[str]:
        """List keys with the given prefix."""
        ...
