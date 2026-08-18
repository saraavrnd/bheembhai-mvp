"""Local filesystem storage — dev-only ObjectStorage backend (ADR-011)."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

from bheembhai.protocols.storage import (
    PresignedUrl,
    StoredHead,
    StoredObject,
)


def _read_range(target: Path, start: int, end: int | None) -> bytes:
    with open(target, "rb") as f:
        f.seek(start)
        if end is None:
            return f.read()
        return f.read(max(0, end - start + 1))


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

    async def put_file(
        self, key: str, path: str, content_type: str | None = None
    ) -> None:
        import shutil

        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)

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

    async def head(self, key: str) -> StoredHead | None:
        target = self._resolve(key)
        if not target.is_file():
            return None
        return StoredHead(key=key, size=target.stat().st_size)

    async def get_range(
        self, key: str, start: int = 0, end: int | None = None
    ) -> bytes:
        target = self._resolve(key)
        if not target.is_file():
            return b""
        return await asyncio.to_thread(_read_range, target, start, end)

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
