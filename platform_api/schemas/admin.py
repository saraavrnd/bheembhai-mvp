"""Pydantic schemas for Admin API — user management, project CRUD, membership management."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── User ──────────────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    id: str
    external_id: str
    auth_provider: str
    email: str
    display_name: str
    platform_role: str
    created_at: str  # ISO-8601
    memberships: list["MembershipBrief"] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MembershipBrief(BaseModel):
    project_id: str
    project_name: str
    role: str


class UpdatePlatformRole(BaseModel):
    platform_role: str = Field(..., min_length=1, max_length=20)


# ── Project ───────────────────────────────────────────────────────────────────


class ProjectCreateAdmin(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    project_manager_id: str | None = None


class ProjectResponseAdmin(BaseModel):
    id: str
    name: str
    owner_id: str
    owner_name: str | None = None
    member_count: int = 0
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)


# ── Membership ────────────────────────────────────────────────────────────────


class MemberAdd(BaseModel):
    user_id: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)


class MemberUpdate(BaseModel):
    role: str = Field(..., min_length=1)


class MemberResponse(BaseModel):
    id: str
    user_id: str
    user_name: str
    user_email: str
    role: str
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}
