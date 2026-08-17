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


@dataclass
class StoredHead:
    """Size metadata for an object, without reading its content."""
    key: str
    size: int


class ObjectStorage(Protocol):
    """Pluggable artifact storage. One implementation per deployment."""

    backend_name: str

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Store an object at the given key."""
        ...

    async def put_file(self, key: str, path: str,
                       content_type: str | None = None) -> None:
        """Store a local file at the given key. Backends may stream from disk
        rather than buffering (log files can be many MB)."""
        ...

    async def get(self, key: str) -> StoredObject | None:
        """Retrieve an object by key. None if not found."""
        ...

    async def head(self, key: str) -> StoredHead | None:
        """Size of an object without reading content. None if not found."""
        ...

    async def get_range(self, key: str, start: int = 0,
                        end: int | None = None) -> bytes:
        """Read a byte range [start, end] (end inclusive, None = EOF). Empty
        bytes when the object is absent or the range is unsatisfiable."""
        ...

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> PresignedUrl:
        """Generate a time-limited pre-signed download URL."""
        ...

    async def list(self, prefix: str) -> AsyncIterator[str]:
        """List keys with the given prefix."""
        ...
