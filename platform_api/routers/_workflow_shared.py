"""Shared workflow/policy helpers — YAML parsing, response builders, PM gate.

Used by both the admin router and the project-scoped (PM) routers; keeps the
routers from importing each other.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import yaml
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from bheembhai.models.project import Project
from bheembhai.models.run import Run
from bheembhai.models.skill import Skill, SkillFile
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow

from platform_api.schemas.admin import (
    PolicyGateSchema,
    PolicyParsed,
    PolicyResponse,
    WorkflowParsed,
    WorkflowResponse,
    WorkflowStepSchema,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── YAML parse helpers ──────────────────────────────────────────────────────


def _parse_workflow_yaml(yaml_content: str) -> WorkflowParsed | None:
    """Parse a workflow YAML string into a structured ``WorkflowParsed``.

    Returns ``None`` if the YAML is malformed or missing required keys.
    """
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    if "workflow" not in raw or "steps" not in raw:
        return None

    steps: list[WorkflowStepSchema] = []
    for s in (raw.get("steps") or []):
        if not isinstance(s, dict):
            continue
        routing = {}
        raw_on = s.get("on")
        if raw_on is None:
            # YAML 1.1 interprets bare 'on' as boolean True — check that too
            raw_on = s.get(True)
        if isinstance(raw_on, dict):
            routing = {str(k): str(v) for k, v in raw_on.items()}
        steps.append(WorkflowStepSchema(
            id=str(s.get("id", "")),
            skill=str(s.get("skill", "")),
            model=str(s.get("model", "medium")),
            label=str(s.get("label", "")),
            deadline=int(s.get("deadline", 900)),
            on=routing,
        ))

    return WorkflowParsed(
        workflow=str(raw.get("workflow", "")),
        version=int(raw.get("version", 1)),
        start=str(raw.get("start", steps[0].id if steps else "")),
        steps=steps,
    )


def _referenced_skill_names(parsed: WorkflowParsed | None) -> set[str]:
    """Skill names a workflow's steps reference.

    Falls back to the step id when ``skill:`` is missing, mirroring the
    engine's ``spec.get("skill", step_id)`` resolution in run_init.
    """
    names: set[str] = set()
    if parsed is None:
        return names
    for step in parsed.steps:
        name = (step.skill or step.id).strip()
        if name:
            names.add(name)
    return names


async def clone_referenced_skills(
    db: "AsyncSession",
    source: Workflow,
    project_id,
) -> None:
    """Clone every platform skill ``source`` references into project scope.

    Used by BOTH copy-to-project endpoints (admin and PM — they must not
    diverge). A name the project already has is left untouched (PM edits win
    over re-cloning); names with no platform template are skipped + logged.
    Only adds/flushes — the caller commits.
    """
    for skill_name in _referenced_skill_names(
        _parse_workflow_yaml(source.yaml_content)
    ):
        project_skill = (
            await db.execute(
                select(Skill).where(
                    Skill.project_id == project_id,
                    Skill.name == skill_name,
                )
            )
        ).scalar_one_or_none()
        if project_skill is not None:
            continue

        platform_skill = (
            await db.execute(
                select(Skill)
                .options(selectinload(Skill.files))
                .where(
                    Skill.project_id.is_(None),
                    Skill.name == skill_name,
                )
            )
        ).scalars().first()
        if platform_skill is None:
            logger.warning(
                "Workflow %s references skill '%s' missing from the platform library — skipped",
                source.id, skill_name,
            )
            continue

        cloned_skill = Skill(
            project_id=project_id,
            name=platform_skill.name,
            description=platform_skill.description,
            model=platform_skill.model,
            compatibility=platform_skill.compatibility,
        )
        db.add(cloned_skill)
        await db.flush()
        for sf in platform_skill.files:
            db.add(SkillFile(
                skill_id=cloned_skill.id,
                path=sf.path,
                content=sf.content,
            ))
        logger.info("Skill '%s' cloned to project %s", skill_name, project_id)


def _parse_policy_yaml(yaml_content: str) -> PolicyParsed | None:
    """Parse a policy YAML string into a structured ``PolicyParsed``.

    Returns ``None`` if the YAML is malformed or missing required keys.
    """
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    if "policy" not in raw:
        return None

    gates: dict[str, PolicyGateSchema] = {}
    raw_gates = raw.get("gates")
    if isinstance(raw_gates, dict):
        for step_id, g in raw_gates.items():
            if isinstance(g, dict):
                on_status = g.get("on_status")
                if isinstance(on_status, list):
                    on_status = [str(x) for x in on_status]
                else:
                    on_status = None
                gates[str(step_id)] = PolicyGateSchema(
                    review=str(g.get("review", "required")),
                    role=str(g.get("role", "any")),
                    on_status=on_status,
                )

    return PolicyParsed(
        policy=str(raw.get("policy", "")),
        version=int(raw.get("version", 1)),
        applies_to=str(raw.get("applies_to", "")),
        gates=gates,
    )


# ── Response builders ───────────────────────────────────────────────────────


async def _workflow_to_response(workflow: Workflow, db: "AsyncSession") -> WorkflowResponse:
    """Build a ``WorkflowResponse`` with parsed YAML, policy count, and run count."""
    # Count policies and runs
    policy_count = (
        await db.execute(
            select(func.count(Policy.id)).where(Policy.workflow_id == workflow.id)
        )
    ).scalar() or 0
    run_count = (
        await db.execute(
            select(func.count(Run.id)).where(Run.workflow_id == workflow.id)
        )
    ).scalar() or 0

    # Project name (nullable — workflows are project-independent templates)
    project_name: str | None = None
    project_id: str = ""
    if workflow.project_id:
        project = await db.get(Project, workflow.project_id)
        project_name = project.name if project else None
        project_id = str(workflow.project_id)

    return WorkflowResponse(
        id=str(workflow.id),
        project_id=project_id,
        project_name=project_name,
        name=workflow.name,
        version=workflow.version,
        is_active=workflow.is_active,
        yaml_content=workflow.yaml_content,
        parsed=_parse_workflow_yaml(workflow.yaml_content),
        policy_count=policy_count,
        run_count=run_count,
        created_at=workflow.created_at.isoformat() if workflow.created_at else "",
    )


def _policy_to_response(policy: Policy, workflow_name: str | None = None) -> PolicyResponse:
    """Build a ``PolicyResponse`` with parsed YAML."""
    return PolicyResponse(
        id=str(policy.id),
        project_id=str(policy.project_id) if policy.project_id else "",
        workflow_id=str(policy.workflow_id),
        workflow_name=workflow_name,
        name=policy.name,
        version=policy.version,
        is_active=policy.is_active,
        yaml_content=policy.yaml_content,
        parsed=_parse_policy_yaml(policy.yaml_content),
        created_at=policy.created_at.isoformat() if policy.created_at else "",
    )


# ── Permission helper ───────────────────────────────────────────────────────


async def _require_pm_of_workflow(
    workflow: Workflow,
    current_user: User,
    db: "AsyncSession",
) -> None:
    """403 unless ``workflow`` is project-scoped and the user manages that project.

    Platform templates (``project_id IS NULL``) are admin-managed, so project
    managers never get write access to them through the project-scoped API.
    """
    if workflow.project_id is None:
        raise HTTPException(403, "Platform templates are managed by administrators")

    membership = (await db.execute(
        select(Membership).where(
            Membership.user_id == current_user.id,
            Membership.project_id == workflow.project_id,
        )
    )).scalar_one_or_none()
    if membership is None or membership.role != "project_manager":
        raise HTTPException(403, "Only a project manager can do this")
