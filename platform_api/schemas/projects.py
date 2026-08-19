"""Pydantic schemas for Project CRUD (minimal — integration management is the focus)."""

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}
