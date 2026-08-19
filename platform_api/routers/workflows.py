"""Workflow and policy endpoints for users.

Members can list workflows/policies (used by the new-run form). Project
managers additionally manage their project's workflows: copy platform
templates, edit, deactivate, delete.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.models.project import Project
from bheembhai.models.run import Run, Transition
from bheembhai.models.user import Membership, User
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.models.workflow_category import WorkflowCategory
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import or_, select

from platform_api.dependencies import get_current_enabled_user
from platform_api.routers._run_stats import _median, _relative_time, _run_stats
from platform_api.routers._workflow_shared import (
    _parse_policy_yaml,
    _parse_workflow_yaml,
    _policy_to_response,
    _require_pm_of_workflow,
    _workflow_to_response,
    clone_referenced_skills,
)
from platform_api.routers.runs import TERMINAL_RUN_STATES
from platform_api.schemas.admin import (
    CopyToProjectRequest,
    PolicyResponse,
    WorkflowResponse,
    WorkflowUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_workflows(
    include_inactive: bool = Query(False),
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[WorkflowResponse]:
    """List workflows.

    - ``project_id=__platform__`` → platform templates only (any authenticated user)
    - ``project_id=<uuid>`` → project-scoped + platform templates (members);
      with ``include_inactive=true`` → project-scoped only incl. inactive (PM)
    - no ``project_id`` → platform templates only
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    is_real_project = bool(project_id and project_id != "__platform__")

    if is_real_project and current_user.platform_role != "ADMIN":
        # Verify membership (platform ADMINs bypass)
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == current_user.id,
                    Membership.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(403, "You are not a member of this project")
        if include_inactive and membership.role != "project_manager":
            raise HTTPException(403, "Only a project manager can do this")

    stmt = select(Workflow).order_by(Workflow.created_at.desc())

    if project_id == "__platform__":
        stmt = stmt.where(Workflow.project_id.is_(None))
    elif is_real_project:
        if include_inactive:
            # PM management view: only this project's workflows
            stmt = stmt.where(Workflow.project_id == project_id)
        else:
            # Member view (new-run form): project-scoped + platform templates
            stmt = stmt.where(
                or_(Workflow.project_id == project_id, Workflow.project_id.is_(None))
            )
    else:
        stmt = stmt.where(Workflow.project_id.is_(None))

    if not (include_inactive and is_real_project):
        stmt = stmt.where(Workflow.is_active == True)

    result = await db.execute(stmt)
    return [await _workflow_to_response(w, db) for w in result.scalars().all()]


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Get a workflow by ID (any authenticated user)."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    return await _workflow_to_response(workflow, db)


@router.get("/{workflow_id}/home")
async def workflow_home(
    workflow_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Workflow home: definition strip, 30-day stats, and scoped run list.

    Project workflows only — platform templates have no runs and are 404'd
    here (which also avoids leaking their ids to non-members).
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")
    if workflow.project_id is None:
        raise HTTPException(404, "Platform templates have no run history")

    membership = (
        await db.execute(
            select(Membership).where(
                Membership.user_id == current_user.id,
                Membership.project_id == workflow.project_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None and current_user.platform_role != "ADMIN":
        raise HTTPException(403, "You are not a member of this project")

    # Newest active policy (same rule as create_run's default policy pick).
    pol_result = await db.execute(
        select(Policy)
        .where(Policy.workflow_id == workflow_id, Policy.is_active == True)
        .order_by(Policy.created_at.desc())
        .limit(1)
    )
    active_policy = pol_result.scalar_one_or_none()
    parsed_policy = _parse_policy_yaml(active_policy.yaml_content) if active_policy else None
    gates: dict[str, dict] = {}
    if parsed_policy:
        for step_id, g in parsed_policy.gates.items():
            gates[str(step_id)] = {
                "review": g.review,
                "role": g.role,
                "on_status": g.on_status,
            }

    # 30-day window for stats + run list.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    wr_result = await db.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id, Run.created_at >= cutoff)
        .order_by(Run.created_at.desc())
    )
    window_runs = wr_result.scalars().all()

    # Paused runs (current awaiting review — NOT windowed: an old run can
    # still be sitting at a gate).
    paused_result = await db.execute(
        select(Run).where(Run.workflow_id == workflow_id, Run.state == "paused")
    )
    paused_runs = paused_result.scalars().all()

    # One transitions query for both sets, grouped per run in memory.
    run_ids = [r.id for r in window_runs] + [r.id for r in paused_runs]
    rows_by_run: dict = {}
    if run_ids:
        tr_result = await db.execute(
            select(Transition)
            .where(Transition.run_id.in_(run_ids))
            .order_by(Transition.id)
        )
        for tr in tr_result.scalars().all():
            rows_by_run.setdefault(tr.run_id, []).append(tr)

    # Per-run counters over the window.
    per_run: dict = {}
    for run in window_runs:
        per_run[run.id] = _run_stats(
            rows_by_run.get(run.id, []), run.created_at.timestamp()
        )

    # Awaiting review: real gate role from the latest awaiting_approval
    # payload, falling back to the active policy's gate role for the step.
    awaiting_items: list[dict] = []
    for run in paused_runs:
        gate_role = None
        for tr in rows_by_run.get(run.id, []):
            # The engine records the gate row as completed→awaiting_approval
            # (state_machine.py) — match on to_state alone, the same rule as
            # the engine's own _last_gate_transition.
            if tr.to_state == "awaiting_approval":
                payload = tr.payload or {}
                gate_role = payload.get("role") or None
        if not gate_role and run.current_step:
            gate_role = gates.get(run.current_step, {}).get("role")
        awaiting_items.append({
            "run_id": str(run.id),
            "story_id": run.story_id,
            "gate_step": run.current_step,
            "gate_role": gate_role,
        })

    # Stats over the window.
    durations = [
        per_run[r.id]["duration_s"]
        for r in window_runs
        if per_run[r.id]["duration_s"] is not None
    ]
    gate_waits = [
        w for r in window_runs for w in per_run[r.id]["gate_waits"]
    ]
    by_state = {s: 0 for s in TERMINAL_RUN_STATES}
    for run in window_runs:
        if run.state in TERMINAL_RUN_STATES:
            by_state[run.state] += 1
    live = sum(1 for r in window_runs if r.state not in TERMINAL_RUN_STATES)
    loop_runs = sum(1 for r in window_runs if per_run[r.id]["loop_backs"] > 0)

    edge_counts: dict = {}
    for r in window_runs:
        for (src, dst), n in per_run[r.id]["loop_edges"].items():
            edge_counts[(src, dst)] = edge_counts.get((src, dst), 0) + n
    most_common = max(edge_counts, key=edge_counts.get) if edge_counts else None

    # Category name for the header.
    category_name = None
    if workflow.workflow_category_id:
        category = await db.get(WorkflowCategory, workflow.workflow_category_id)
        category_name = category.name if category else None

    runs_list = []
    for run in window_runs[:20]:
        rows = rows_by_run.get(run.id, [])
        updated_ts = max((float(tr.ts) for tr in rows), default=None)
        runs_list.append({
            "run_id": str(run.id),
            "story_id": run.story_id,
            "state": run.state,
            "needs_review": run.state == "paused",
            "updated": _relative_time(updated_ts) if updated_ts is not None
            else None,
            "executions": per_run[run.id]["executions"],
            "loop_backs": per_run[run.id]["loop_backs"],
        })

    parsed = _parse_workflow_yaml(workflow.yaml_content)
    return {
        "workflow": {
            "id": str(workflow.id),
            "project_id": str(workflow.project_id),
            "name": workflow.name,
            "version": workflow.version,
            "description": workflow.description or "",
            "is_active": workflow.is_active,
            "category_id": str(workflow.workflow_category_id)
            if workflow.workflow_category_id else "",
            "category_name": category_name,
            "parsed": parsed,
        },
        "active_policy": {
            "id": str(active_policy.id),
            "name": active_policy.name,
            "version": active_policy.version,
            "gates": gates,
        } if active_policy else None,
        "stats": {
            "runs_total": len(window_runs),
            "by_state": by_state,
            "live": live,
            "median_duration_s": _median(durations),
            "median_gate_wait_s": _median(gate_waits),
            "loop_back_rate_pct": round(100 * loop_runs / len(window_runs), 1)
            if window_runs else 0,
            "most_common_loop_edge": {
                "from_step": most_common[0],
                "to_step": most_common[1],
                "count": edge_counts[most_common],
            } if most_common else None,
            "awaiting_review": {"total": len(awaiting_items), "items": awaiting_items},
        },
        "runs": runs_list,
    }


@router.get("/{workflow_id}/policies")
async def list_policies(
    workflow_id: str,
    include_inactive: bool = Query(False),
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[PolicyResponse]:
    """List policies for a workflow.

    Default: active policies only (any authenticated user — used by the
    new-run form). ``include_inactive=true`` additionally requires the
    ``project_manager`` role on the workflow's project.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    if include_inactive:
        await _require_pm_of_workflow(workflow, current_user, db)

    stmt = (
        select(Policy)
        .where(Policy.workflow_id == workflow_id)
        .order_by(Policy.created_at.desc())
    )
    if not include_inactive:
        stmt = stmt.where(Policy.is_active == True)

    result = await db.execute(stmt)
    return [_policy_to_response(p, workflow.name) for p in result.scalars().all()]


@router.post("/{workflow_id}/copy-to-project", status_code=201)
async def copy_workflow_to_project(
    workflow_id: str,
    body: CopyToProjectRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Clone a platform workflow (and its policies) to a project.

    Requires the ``project_manager`` role in the target project. The source
    workflow remains as a platform template; the clone gets ``project_id``
    set so the project can customise it independently.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    source = await db.get(Workflow, workflow_id)
    if source is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")
    if source.project_id is not None:
        raise HTTPException(403, "Only platform templates can be copied to a project")

    # PM check on the target project (platform ADMINs bypass)
    if current_user.platform_role != "ADMIN":
        membership = (
            await db.execute(
                select(Membership).where(
                    Membership.user_id == current_user.id,
                    Membership.project_id == body.project_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None or membership.role != "project_manager":
            raise HTTPException(403, "Only a project manager can do this")

    project = await db.get(Project, body.project_id)
    if project is None:
        raise HTTPException(404, f"Project {body.project_id} not found")

    # Check for duplicate (project_id, name, version) — project-scoped uniqueness
    existing = (
        await db.execute(
            select(Workflow).where(
                Workflow.project_id == body.project_id,
                Workflow.name == source.name,
                Workflow.version == source.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"A workflow named '{source.name}' version {source.version} already exists in project '{project.name}'",
        )

    # Clone the workflow
    clone = Workflow(
        project_id=body.project_id,
        name=source.name,
        description=source.description,
        version=source.version,
        yaml_content=source.yaml_content,
        is_active=True,
        workflow_category_id=source.workflow_category_id,
    )
    db.add(clone)
    await db.flush()

    # Clone associated policies
    policies_result = await db.execute(
        select(Policy).where(Policy.workflow_id == source.id)
    )
    for pol in policies_result.scalars().all():
        db.add(Policy(
            project_id=body.project_id,
            workflow_id=clone.id,
            name=pol.name,
            version=pol.version,
            yaml_content=pol.yaml_content,
            is_active=pol.is_active,
        ))

    # Clone referenced platform skills into project-scoped rows (shared helper
    # with the admin copy endpoint — they must not diverge). The store
    # publishes each fresh clone's bundle (publish-on-write).
    await clone_referenced_skills(
        db, source, body.project_id,
        store=getattr(request.app.state, "object_store", None),
    )

    await db.commit()

    logger.info(
        "Workflow cloned by PM: %s → %s (project=%s)",
        source.id, clone.id, body.project_id,
    )
    return await _workflow_to_response(clone, db)


@router.patch("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdate,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> WorkflowResponse:
    """Update a project-scoped workflow's name, YAML, or active status.

    Requires the ``project_manager`` role in the workflow's project.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    await _require_pm_of_workflow(workflow, current_user, db)

    if body.name is not None:
        name = body.name.strip()
        # Check for duplicate (excluding self) — uniqueness within the project
        existing = (
            await db.execute(
                select(Workflow).where(
                    Workflow.project_id == workflow.project_id,
                    Workflow.name == name,
                    Workflow.version == workflow.version,
                    Workflow.id != workflow.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                409,
                f"A workflow named '{name}' version {workflow.version} already exists in this project",
            )
        workflow.name = name
    if body.yaml_content is not None:
        workflow.yaml_content = body.yaml_content
        # Update version from parsed YAML if present
        parsed = _parse_workflow_yaml(body.yaml_content)
        if parsed:
            workflow.version = parsed.version
    if body.is_active is not None:
        workflow.is_active = body.is_active
    if body.description is not None:
        workflow.description = body.description.strip()
    # Key present → set the category (clearing is rejected — workflows must
    # always belong to a category); absent → unchanged.
    if "category_id" in body.model_fields_set:
        if not body.category_id:
            raise HTTPException(
                400, "Category is required — workflows must belong to a category"
            )
        category = await db.get(WorkflowCategory, body.category_id)
        if category is None:
            raise HTTPException(400, f"Category {body.category_id} not found")
        workflow.workflow_category_id = category.id

    await db.commit()

    logger.info("Workflow updated by PM: %s", workflow_id)
    return await _workflow_to_response(workflow, db)


@router.delete("/{workflow_id}")
async def delete_workflow(
    workflow_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
):
    """Delete a project-scoped workflow and all associated policies and runs.

    Children are deleted explicitly because the FK columns
    (``runs.workflow_id``, ``runs.policy_id``, ``policies.workflow_id``)
    lack ``ON DELETE CASCADE`` at the database level.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    workflow = await db.get(Workflow, workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {workflow_id} not found")

    await _require_pm_of_workflow(workflow, current_user, db)

    # 1. Delete runs that reference this workflow (CASCADE takes care of
    #    steps, transitions, and work-queue items).
    runs_result = await db.execute(
        select(Run).where(Run.workflow_id == workflow.id)
    )
    for run in runs_result.scalars().all():
        await db.delete(run)

    # 2. Delete policies that reference this workflow.
    policies_result = await db.execute(
        select(Policy).where(Policy.workflow_id == workflow.id)
    )
    for pol in policies_result.scalars().all():
        await db.delete(pol)

    # 3. Now safe to delete the workflow itself.
    await db.delete(workflow)
    await db.commit()

    logger.info("Workflow deleted by PM: %s name=%s", workflow_id, workflow.name)
    return Response(status_code=204)
