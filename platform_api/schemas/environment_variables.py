"""Pydantic schemas for environment-variable CRUD.

Plain rows store ``value``; secret rows store an opaque SecureStorage
``credential_ref`` — the raw secret is handed to SecureStorage on write and
NEVER returned. Responses for secret rows carry ``value=None`` +
``has_value=True``. ``value_type`` is immutable: converting a variable means
deleting and re-creating it, so Update omits (and forbids) the field.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class EnvVarCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    value_type: Literal["plain", "secret"]
    value: str | None = Field(None, min_length=1, max_length=8192)
    description: str | None = Field(None, max_length=512)

    model_config = {"extra": "forbid"}


class EnvVarUpdate(BaseModel):
    """All fields optional; only send what changed. A present ``value`` means
    plain value replace or secret rotation (same SecureStorage ref)."""

    name: str | None = Field(None, min_length=1, max_length=64)
    value: str | None = Field(None, min_length=1, max_length=8192)
    description: str | None = Field(None, max_length=512)

    model_config = {"extra": "forbid"}


class EnvVarResponse(BaseModel):
    """Public-facing variable — secret values are NEVER included."""

    id: str
    name: str
    scope: str  # platform | project
    source: str  # 'platform' (platform row, shown read-only in projects) | 'project'
    value_type: str  # plain | secret
    value: str | None  # plain only; None for secrets
    has_value: bool
    overridden: bool  # platform row: a project row shares this name
    overrides_platform: bool  # project row: shadows a platform row's name
    description: str | None
    created_at: datetime
