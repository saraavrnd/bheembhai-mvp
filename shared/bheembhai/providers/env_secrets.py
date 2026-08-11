"""Env-based secure storage — dev-only SecureStorage backend (ADR-012).

Stores credentials in an in-process dict keyed by ref.  Not durable across
restarts — this is intentional for DEV.  Production swaps to aws_ssm or
aws_secrets_manager by changing SECURE_STORAGE_BACKEND.
"""

from __future__ import annotations

from bheembhai.protocols.secrets import Credential

# In-process store — lives for the lifetime of the process, never persisted.
_store: dict[str, str] = {}


class EnvSecureStorage:
    """In-memory credential store for dev / demos.  Also falls back to env vars.

    Credential refs are free-form strings (e.g. ``github-token``).  ``put``
    writes to the in-process dict; ``get`` checks the dict first, then falls
    back to ``os.environ`` so you can pre-seed secrets via ``.env`` as well.
    """

    backend_name = "env"

    def __init__(self, encrypted_config_path: str = "") -> None:
        self._encrypted_config_path = encrypted_config_path

    async def get(self, ref: str) -> Credential | None:
        import os

        # 1. In-process store (values created via the API)
        if ref in _store:
            return Credential(ref=ref, value=_store[ref], provider="env")

        # 2. Fallback: env-var-style refs ("env:VAR_NAME" or bare VAR_NAME)
        var_name = ref.removeprefix("env:")
        value = os.getenv(var_name)
        if value is not None:
            return Credential(ref=ref, value=value, provider="env")

        return None

    async def put(
        self, ref: str, value: str, metadata: dict | None = None
    ) -> str:
        _store[ref] = value
        return ref

    async def delete(self, ref: str) -> None:
        _store.pop(ref, None)
