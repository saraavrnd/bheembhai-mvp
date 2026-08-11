"""Project Integration CRUD endpoints.

Every endpoint that accepts a credential_VALUE immediately hands it to
SecureStorage and discards it — only the opaque credential_ref is persisted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from bheembhai.database import get_session
from bheembhai.models.project import Project, ProjectIntegration
from bheembhai.protocols.auth import Identity

from platform_api.dependencies import get_current_user
from platform_api.schemas.integrations import (
    IntegrationCreate,
    IntegrationResponse,
    IntegrationUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/integrations", tags=["integrations"])


# ── Helpers ──────────────────────────────────────────────────────────────────


def _secure_storage(request: Request):
    """Return the SecureStorage provider wired at startup."""
    ss = getattr(request.app.state, "secure_storage", None)
    if ss is None:
        raise HTTPException(500, "Secure storage backend is not configured")
    return ss


async def _get_project_or_404(project_id: str, db: "AsyncSession") -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return project


def _to_response(integ: ProjectIntegration) -> IntegrationResponse:
    return IntegrationResponse(
        id=str(integ.id),
        project_id=str(integ.project_id),
        type=integ.type,
        label=integ.label,
        credential_ref=integ.credential_ref,
        config=integ.config or {},
        verified_at=integ.verified_at,
        created_at=integ.created_at,
    )


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_integration(
    project_id: str,
    body: IntegrationCreate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> IntegrationResponse:
    """Create a new integration for a project.

    The raw ``credential_value`` is written to SecureStorage immediately;
    only an opaque reference is stored in the database.
    """
    project = await _get_project_or_404(project_id, db)
    secure = _secure_storage(request)

    # ── Check for duplicate type+label ────────────────────────────
    existing = (
        await db.execute(
            select(ProjectIntegration).where(
                ProjectIntegration.project_id == project.id,
                ProjectIntegration.type == body.type,
                ProjectIntegration.label == body.label,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        raise HTTPException(
            409,
            f"Integration '{body.label}' of type '{body.type}' already exists on this project",
        )

    # ── Store secret → get ref → discard value ────────────────────
    ref = await secure.put(
        ref=f"/bheembhai/{project_id}/{body.type}/{body.label}",
        value=body.credential_value,
        metadata={"project_id": project_id, "type": body.type, "label": body.label},
    )

    # ── Persist only the pointer ──────────────────────────────────
    integration = ProjectIntegration(
        project_id=project.id,
        type=body.type,
        label=body.label,
        credential_ref=ref,
        config=body.config,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)

    logger.info(
        "Integration created: %s type=%s label=%s ref=%s",
        integration.id, body.type, body.label, ref,
    )
    return _to_response(integration)


@router.get("")
async def list_integrations(
    project_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> list[IntegrationResponse]:
    """List all integrations for a project. Credential values are NEVER returned."""
    await _get_project_or_404(project_id, db)

    result = await db.execute(
        select(ProjectIntegration)
        .where(ProjectIntegration.project_id == project_id)
        .order_by(ProjectIntegration.created_at)
    )
    return [_to_response(row) for row in result.scalars().all()]


@router.get("/{integration_id}")
async def get_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> IntegrationResponse:
    """Get a single integration by ID. Credential value is NEVER returned."""
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    return _to_response(integration)


@router.patch("/{integration_id}")
async def update_integration(
    project_id: str,
    integration_id: str,
    body: IntegrationUpdate,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
) -> IntegrationResponse:
    """Update an integration's label, config, or rotate its credential."""
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    # ── Rotate credential if a new value was provided ─────────────
    if body.credential_value is not None:
        secure = _secure_storage(request)
        await secure.put(
            ref=integration.credential_ref,
            value=body.credential_value,
            metadata={"project_id": project_id, "type": integration.type, "label": integration.label},
        )
        logger.info("Credential rotated for integration %s", integration_id)

    if body.label is not None:
        integration.label = body.label
    if body.config is not None:
        integration.config = body.config

    await db.commit()
    await db.refresh(integration)
    return _to_response(integration)


@router.delete("/{integration_id}")
async def delete_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: "AsyncSession" = Depends(get_session),
    user: Identity | None = Depends(get_current_user),
):
    """Delete an integration and its stored credential."""
    await _get_project_or_404(project_id, db)

    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")

    # ── Wipe the secret from SecureStorage ────────────────────────
    secure = _secure_storage(request)
    await secure.delete(integration.credential_ref)

    await db.delete(integration)
    await db.commit()

    logger.info("Integration deleted: %s ref=%s", integration_id, integration.credential_ref)
    return Response(status_code=204)
