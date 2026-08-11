"""Local filesystem storage — dev-only ObjectStorage backend (ADR-011)."""

import os
from pathlib import Path
from typing import AsyncIterator

from bheembhai.protocols.storage import ObjectStorage, PresignedUrl, StoredObject


class LocalStorage:
    """Writes artifacts to the local filesystem. Dev/testing only."""

    backend_name = "local"

    def __init__(self, base_path: str = "/tmp/bheembhai-artifacts") -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        # Guard against path traversal
        safe = Path(key).as_posix().lstrip("/")
        return self.base_path / safe

    async def put(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def get(self, key: str) -> StoredObject | None:
        target = self._resolve(key)
        if not target.exists():
            return None
        return StoredObject(
            key=key,
            data=target.read_bytes(),
            content_type=None,
            metadata=None,
        )

    async def presigned_get_url(
        self, key: str, expires_in: int = 3600
    ) -> PresignedUrl:
        import time
        return PresignedUrl(
            url=f"file://{self._resolve(key)}",
            expires_at=time.time() + expires_in,
        )

    async def list(self, prefix: str) -> AsyncIterator[str]:
        base = self._resolve(prefix)
        if not base.exists():
            return
        for root, _dirs, files in os.walk(base):
            for f in files:
                full = Path(root) / f
                rel = full.relative_to(self.base_path).as_posix()
                yield rel
