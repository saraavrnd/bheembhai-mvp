"""Pydantic schemas for Admin API — user management, project CRUD, membership management."""

from __future__ import annotations

from pydantic import BaseModel, Field

# ── User ──────────────────────────────────────────────────────────────────────


class UserResponse(BaseModel):
    id: str
    external_id: str
    auth_provider: str
    email: str
    display_name: str
    platform_role: str
    is_enabled: bool
    created_at: str  # ISO-8601
    memberships: list[MembershipBrief] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MembershipBrief(BaseModel):
    project_id: str
    project_name: str
    role: str


class UpdatePlatformRole(BaseModel):
    platform_role: str = Field(..., min_length=1, max_length=20)


class UpdateUserEnabled(BaseModel):
    is_enabled: bool


# ── Project ───────────────────────────────────────────────────────────────────


class ProjectCreateAdmin(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    project_manager_id: str = Field(..., min_length=1)


class ProjectResponseAdmin(BaseModel):
    id: str
    name: str
    description: str = ""
    owner_id: str
    owner_name: str | None = None
    member_count: int = 0
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=500)


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


# ── Skills ────────────────────────────────────────────────────────────────────


class SkillFileResponse(BaseModel):
    id: str
    path: str
    content: str
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}


class SkillResponse(BaseModel):
    id: str
    name: str
    description: str
    model: str
    compatibility: str | None = None
    created_at: str  # ISO-8601
    updated_at: str  # ISO-8601
    files: list[SkillFileResponse] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1)
    model: str = Field(default="medium", pattern=r"^(high|medium|low)$")
    compatibility: str | None = None


class SkillUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    model: str | None = Field(None, pattern=r"^(high|medium|low)$")
    compatibility: str | None = None


class SkillFileCreate(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)


class SkillFileUpdate(BaseModel):
    content: str = Field(..., min_length=1)


# ── Skill zip import ────────────────────────────────────────────────────────


class SkillImportSkillAnalysis(BaseModel):
    """One row of the import analysis table (skill | dependent files | existing?)."""

    name: str
    directory: str
    description: str
    model: str
    compatibility: str | None = None
    warnings: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)  # dependent files incl. zip-backed external refs
    file_contents: dict[str, str] = Field(default_factory=dict)  # path → content (files preview)
    missing_referenced: list[str] = Field(default_factory=list)
    external_references: list[str] = Field(default_factory=list)  # in zip, outside the skill dir
    exists: bool = False


class SkillImportAnalyzeResponse(BaseModel):
    skills: list[SkillImportSkillAnalysis] = Field(default_factory=list)
    invalid_dirs: list[str] = Field(default_factory=list)
    other_entries: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SkillImportResult(BaseModel):
    name: str
    action: str  # import | overwrite | skip
    status: str  # imported | overwritten | skipped | error
    message: str | None = None
    skill_id: str | None = None


class SkillImportResponse(BaseModel):
    results: list[SkillImportResult] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)


# ── Workflow categories ────────────────────────────────────────────────────


class WorkflowCategoryResponse(BaseModel):
    id: str
    name: str
    description: str = ""
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}


class WorkflowCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class WorkflowCategoryUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)


# ── Workflows ───────────────────────────────────────────────────────────────


class WorkflowStepSchema(BaseModel):
    """A single step within a parsed workflow YAML."""
    id: str
    skill: str
    model: str = "medium"
    label: str = ""
    deadline: int = 900
    on: dict[str, str] = Field(default_factory=dict)


class WorkflowParsed(BaseModel):
    """Structured representation of a workflow YAML."""
    workflow: str
    version: int = 1
    start: str
    steps: list[WorkflowStepSchema] = Field(default_factory=list)


class WorkflowResponse(BaseModel):
    id: str
    project_id: str
    project_name: str | None = None
    name: str
    version: int
    description: str = ""
    is_active: bool
    yaml_content: str
    parsed: WorkflowParsed | None = None
    policy_count: int = 0
    run_count: int = 0
    created_at: str  # ISO-8601
    category_id: str = ""  # empty string = uncategorized (project_id convention)
    category_name: str | None = None

    model_config = {"from_attributes": True}


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    yaml_content: str = Field(default="", min_length=0)
    category_id: str  # required — every workflow must belong to a category


class WorkflowUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)
    yaml_content: str | None = None
    is_active: bool | None = None
    # Key present (even null) → set/clear; absent → unchanged (model_fields_set)
    category_id: str | None = None


# ── Policies ────────────────────────────────────────────────────────────────


class PolicyGateSchema(BaseModel):
    review: str = "required"
    role: str = "any"
    on_status: list[str] | None = None


class PolicyParsed(BaseModel):
    policy: str
    version: int = 1
    applies_to: str
    gates: dict[str, PolicyGateSchema] = Field(default_factory=dict)


class PolicyResponse(BaseModel):
    id: str
    project_id: str
    workflow_id: str
    workflow_name: str | None = None
    name: str
    version: int
    is_active: bool
    yaml_content: str
    parsed: PolicyParsed | None = None
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}


class PolicyCreate(BaseModel):
    workflow_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    yaml_content: str = Field(..., min_length=1)


class PolicyUpdate(BaseModel):
    yaml_content: str | None = None
    is_active: bool | None = None


# ── Shared utilities ────────────────────────────────────────────────────────


class SkillNameResponse(BaseModel):
    id: str
    name: str


class RoleResponse(BaseModel):
    key: str
    label: str


class CopyToProjectRequest(BaseModel):
    project_id: str = Field(..., min_length=1)


# ── Integrations ──────────────────────────────────────────────────────────────


class IntegrationTypeMeta(BaseModel):
    """Describes one integration type for the UI type registry."""
    key: str
    label: str
    category: str  # "TOOLS" or "AI VENDORS"
    icon: str  # two-letter code, e.g. "JR", "GH", "OA"
    description: str = ""
    fields: list[str] = Field(default_factory=list)  # ordered field names for the form


class IntegrationFieldDef(BaseModel):
    """Describes a single form field for an integration type."""
    name: str
    label: str
    field_type: str = "text"  # text | secret | select
    required: bool = False
    placeholder: str = ""
    options: list[dict[str, str]] | None = None  # for select fields


class IntegrationAdminResponse(BaseModel):
    """Integration row returned to the admin UI — credential VALUE is NEVER included."""
    id: str = ""
    project_id: str = ""
    type: str
    label: str = ""
    credential_ref: str = ""
    config: dict = Field(default_factory=dict)
    verified_at: str | None = None  # ISO-8601
    created_at: str = ""  # ISO-8601
    status: str = "unconfigured"  # connected | warning | unconfigured

    model_config = {"from_attributes": True}


class IntegrationAdminCreate(BaseModel):
    """Payload for creating/updating an integration from the admin form."""
    type: str = Field(..., min_length=1, max_length=32)
    label: str = Field(default="", max_length=128)
    credential_value: str = Field(default="", max_length=4096)
    config: dict = Field(default_factory=dict)


class IntegrationAdminUpdate(BaseModel):
    """Payload for partial update of an integration from the admin form."""
    label: str | None = Field(None, max_length=128)
    credential_value: str | None = Field(None, max_length=4096)
    config: dict | None = None


class TestConnectionResult(BaseModel):
    """Result of a connection test."""
    ok: bool
    message: str
    details: dict = Field(default_factory=dict)
