"""Project-scoped integration endpoints (member read / project-manager write).

Every endpoint that accepts a credential_VALUE immediately hands it to
SecureStorage and discards it — only the opaque credential_ref is persisted.

Permissions:
- GET list / single: any project member
- POST / PATCH / DELETE / test: project_manager only

POST is upsert-by-type: saving the same integration type again updates the
existing row in place (mirrors the admin form behaviour), so repeated saves
never 409.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.models.project import ProjectIntegration
from bheembhai.models.user import User
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select

from platform_api.dependencies import require_project_manager, require_project_member
from platform_api.routers._integration_shared import (
    INTEGRATION_TYPE_REGISTRY,
    _integration_to_response,
    _secure_storage,
    _test_integration_connection,
    validate_ai_vendor_config,
)
from platform_api.schemas.admin import (
    IntegrationAdminCreate,
    IntegrationAdminResponse,
    IntegrationAdminUpdate,
    TestConnectionResult,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects/{project_id}/integrations", tags=["integrations"])


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_integration_or_404(
    project_id: str, integration_id: str, db: AsyncSession
) -> ProjectIntegration:
    integration = await db.get(ProjectIntegration, integration_id)
    if integration is None or str(integration.project_id) != project_id:
        raise HTTPException(404, f"Integration {integration_id} not found")
    return integration


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_integrations(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> list[IntegrationAdminResponse]:
    """List all integrations for a project. Credential values are NEVER returned."""
    result = await db.execute(
        select(ProjectIntegration)
        .where(ProjectIntegration.project_id == project_id)
        .order_by(ProjectIntegration.created_at)
    )
    return [_integration_to_response(row) for row in result.scalars().all()]


@router.get("/{integration_id}")
async def get_integration(
    project_id: str,
    integration_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> IntegrationAdminResponse:
    """Get a single integration by ID. Credential value is NEVER returned."""
    integration = await _get_integration_or_404(project_id, integration_id, db)
    return _integration_to_response(integration)


@router.post("", status_code=201)
async def create_integration(
    project_id: str,
    body: IntegrationAdminCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> IntegrationAdminResponse:
    """Create or overwrite an integration for a project.

    If an integration of the same type already exists it is updated in-place
    (upsert-by-type) — the raw ``credential_value`` goes to SecureStorage
    immediately and only an opaque reference is persisted.
    """
    if body.type not in INTEGRATION_TYPE_REGISTRY:
        raise HTTPException(400, f"Unknown integration type: {body.type}")

    # Check for existing integration of this type
    existing = (
        await db.execute(
            select(ProjectIntegration).where(
                ProjectIntegration.project_id == project_id,
                ProjectIntegration.type == body.type,
            )
        )
    ).scalar_one_or_none()

    # AI vendors must map all three model tiers before they can be used.
    # Validate the *effective* config: a label-only save on an existing
    # integration must not fail because body.config was omitted.
    effective_config = body.config if body.config else (existing.config if existing else {})
    validate_ai_vendor_config(body.type, effective_config)

    if existing is not None:
        # Update in-place
        if body.label:
            existing.label = body.label
        if body.config:
            existing.config = body.config
        if body.credential_value:
            secure = _secure_storage(request)
            ref = existing.credential_ref or f"/bheembhai/{project_id}/{body.type}/default"
            await secure.put(
                ref=ref,
                value=body.credential_value,
                metadata={"project_id": project_id, "type": body.type, "label": existing.label},
            )
            existing.credential_ref = ref
        await db.commit()
        await db.refresh(existing)
        logger.info("Integration updated: %s type=%s", existing.id, body.type)
        return _integration_to_response(existing)

    # Create new — only touch SecureStorage if a credential was provided
    credential_value = body.credential_value or ""
    ref = ""
    if credential_value:
        secure = _secure_storage(request)
        ref = await secure.put(
            ref=f"/bheembhai/{project_id}/{body.type}/default",
            value=credential_value,
            metadata={"project_id": project_id, "type": body.type, "label": body.label},
        )

    integration = ProjectIntegration(
        project_id=project_id,
        type=body.type,
        label=body.label or body.type,
        credential_ref=ref,
        config=body.config,
    )
    db.add(integration)
    await db.commit()
    await db.refresh(integration)

    logger.info("Integration created: %s type=%s label=%s", integration.id, body.type, integration.label)
    return _integration_to_response(integration)


@router.patch("/{integration_id}")
async def update_integration(
    project_id: str,
    integration_id: str,
    body: IntegrationAdminUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> IntegrationAdminResponse:
    """Update an integration's label, config, or rotate its credential."""
    integration = await _get_integration_or_404(project_id, integration_id, db)

    # ── Rotate credential if a new value was provided ─────────────
    if body.credential_value:
        secure = _secure_storage(request)
        ref = integration.credential_ref or f"/bheembhai/{project_id}/{integration.type}/default"
        await secure.put(
            ref=ref,
            value=body.credential_value,
            metadata={"project_id": project_id, "type": integration.type, "label": integration.label},
        )
        integration.credential_ref = ref
        logger.info("Credential rotated for integration %s", integration_id)

    if body.label is not None:
        integration.label = body.label
    if body.config is not None:
        # AI vendors must map all three model tiers before they can be used
        validate_ai_vendor_config(integration.type, body.config)
        integration.config = body.config

    await db.commit()
    await db.refresh(integration)
    return _integration_to_response(integration)


@router.delete("/{integration_id}")
async def delete_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
):
    """Delete an integration and its stored credential."""
    integration = await _get_integration_or_404(project_id, integration_id, db)

    # ── Wipe the secret from SecureStorage ────────────────────────
    if integration.credential_ref:
        secure = _secure_storage(request)
        await secure.delete(integration.credential_ref)

    await db.delete(integration)
    await db.commit()

    logger.info("Integration deleted: %s type=%s", integration_id, integration.type)
    return Response(status_code=204)


@router.post("/{integration_id}/test")
async def test_integration(
    project_id: str,
    integration_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> TestConnectionResult:
    """Test connectivity for an integration (project manager only).

    Attempts a lightweight authenticated API call based on integration type
    and updates ``verified_at`` on success.
    """
    integration = await _get_integration_or_404(project_id, integration_id, db)

    # Fetch the credential from SecureStorage
    credential_value = ""
    if integration.credential_ref:
        try:
            secure = _secure_storage(request)
            cred = await secure.get(integration.credential_ref)
            credential_value = cred.value if cred else ""
        except Exception:
            logger.debug(
                "test_connection: secure storage fetch failed for ref=%s integration=%s",
                integration.credential_ref, integration_id, exc_info=True,
            )
            credential_value = ""

    if not credential_value:
        logger.debug(
            "test_connection: no credential available for integration=%s ref=%s",
            integration_id, integration.credential_ref or "<none>",
        )
        return TestConnectionResult(ok=False, message="No credential stored — please save an API token first.")

    result = await _test_integration_connection(integration, credential_value)

    # On successful test, update verified_at
    if result.ok:
        integration.verified_at = datetime.now(timezone.utc)
        await db.commit()

    return result
