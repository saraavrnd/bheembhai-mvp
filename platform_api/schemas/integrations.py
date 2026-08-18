"""Pydantic schemas for ProjectIntegration CRUD.

These are the API-facing shapes. The DB model (ProjectIntegration) stores a
``credential_ref`` pointer; these schemas carry the raw ``credential_value``
ONLY on create/update — it is immediately handed to SecureStorage and never returned.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class IntegrationCreate(BaseModel):
    """Payload for creating a new integration.

    ``credential_value`` is the raw secret (API key, token).  It is written to
    SecureStorage and replaced with a ``credential_ref`` before the row is inserted
    — the raw value is never persisted in the DB.
    """

    type: str = Field(
        ...,
        description="Integration type: 'github', 'jira', 'openai', 'claude', 'deepseek', 'kimi'",
        examples=["github"],
    )
    label: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable label, unique per project+type",
        examples=["Production GitHub"],
    )
    credential_value: str = Field(
        ...,
        min_length=1,
        description="Raw secret (API key / token). Stored in SecureStorage, never in the DB.",
    )
    config: dict = Field(
        default_factory=dict,
        description="Optional integration-specific settings (base URLs, model tiers, etc.)",
        examples=[{"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"}],
    )


class IntegrationUpdate(BaseModel):
    """Payload for updating an existing integration.

    All fields are optional — only send what changed.  If ``credential_value`` is
    provided the old secret is overwritten in SecureStorage (rotation).
    """

    label: str | None = Field(None, min_length=1, max_length=128)
    credential_value: str | None = Field(None, min_length=1)
    config: dict | None = None


class IntegrationResponse(BaseModel):
    """Public-facing integration — the credential VALUE is NEVER included."""

    id: str
    project_id: str
    type: str
    label: str
    credential_ref: str  # opaque pointer, NOT the secret
    config: dict
    verified_at: datetime | None
    created_at: datetime
    status: str = "unconfigured"  # connected | warning | unconfigured

    model_config = {"from_attributes": True}
