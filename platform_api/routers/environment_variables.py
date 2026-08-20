"""Environment-variable endpoints: project-scoped CRUD + admin (platform scope).

Scope + override model: platform rows (project_id NULL) apply to every run;
a project row sharing a platform row's name is the override. The project GET
returns a merged view (platform rows marked read-only, override flags set);
writes through the project router only ever touch project rows — platform
rows are managed on the admin router.

Secret model (ADR-012): the raw value is handed to SecureStorage on write
under ``/bheembhai/env/{project_id|platform}/{name}`` and discarded — only
the opaque ref is persisted. Responses never include secret values.

Permissions: project GET = member; project writes = project_manager;
admin router = platform ADMIN.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.env_vars import (
    env_var_ref,
    validate_env_var_name,
    validate_tunable_value,
)
from bheembhai.models.environment import EnvironmentVariable
from bheembhai.models.user import User
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from platform_api.dependencies import (
    require_admin,
    require_project_manager,
    require_project_member,
)
from platform_api.routers._integration_shared import _secure_storage
from platform_api.schemas.environment_variables import (
    EnvVarCreate,
    EnvVarResponse,
    EnvVarUpdate,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/projects/{project_id}/environment-variables",
    tags=["environment-variables"],
)
admin_router = APIRouter(
    prefix="/api/admin/environment-variables", tags=["admin"]
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _row_to_response(row: EnvironmentVariable, *, source: str,
                     overridden: bool = False,
                     overrides_platform: bool = False) -> EnvVarResponse:
    """Public shape — plain values are config and returned; secret values
    never are (value=None, has_value=True)."""
    return EnvVarResponse(
        id=str(row.id),
        name=row.name,
        scope=row.scope,
        source=source,
        value_type=row.value_type,
        value=row.value if row.value_type == "plain" else None,
        has_value=bool(row.value is not None or row.credential_ref),
        overridden=overridden,
        overrides_platform=overrides_platform,
        description=row.description,
        created_at=row.created_at,
    )


def _validate_name(name: str) -> str:
    try:
        return validate_env_var_name(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _validate_value(name: str, value: str) -> None:
    try:
        validate_tunable_value(name, value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


async def _get_envvar_or_404(db: AsyncSession, envvar_id: str,
                             *, project_id: str | None = None,
                             scope: str | None = None) -> EnvironmentVariable:
    row = await db.get(EnvironmentVariable, envvar_id)
    if row is None:
        raise HTTPException(404, f"Environment variable {envvar_id} not found")
    if project_id is not None and str(row.project_id) != project_id:
        raise HTTPException(404, f"Environment variable {envvar_id} not found")
    if scope is not None and row.scope != scope:
        raise HTTPException(404, f"Environment variable {envvar_id} not found")
    return row


async def _check_duplicate(db: AsyncSession, project_id: str | None,
                           name: str, *, exclude_id=None) -> None:
    q = select(EnvironmentVariable).where(
        EnvironmentVariable.name == name,
        EnvironmentVariable.project_id == project_id,
    )
    if exclude_id is not None:
        q = q.where(EnvironmentVariable.id != exclude_id)
    if (await db.execute(q)).scalar_one_or_none() is not None:
        raise HTTPException(
            409, f"Environment variable '{name}' already exists")


async def _store_secret(request: Request, project_id: str | None, name: str,
                        value: str) -> str:
    """PUT the raw secret into SecureStorage and return the opaque ref."""
    secure = _secure_storage(request)
    return await secure.put(
        ref=env_var_ref(project_id, name),
        value=value,
        metadata={"project_id": str(project_id) if project_id else None,
                  "name": name},
    )


async def _commit_or_conflict(db: AsyncSession, name: str) -> None:
    """Commit, translating a concurrent duplicate insert into 409."""
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            409, f"Environment variable '{name}' already exists") from exc


async def _rename_row(request: Request, db: AsyncSession, row: EnvironmentVariable,
                      new_name: str) -> None:
    """Rename with uniqueness re-check; a secret row migrates its stored
    secret to the new ref (get old → put new → delete old). If the old secret
    is already gone from SecureStorage, still move the ref — the engine fails
    fast at run init, which is the designed surface for that error."""
    await _check_duplicate(db, row.project_id, new_name, exclude_id=row.id)
    old_ref = row.credential_ref
    if row.value_type == "secret" and old_ref:
        secure = _secure_storage(request)
        cred = await secure.get(old_ref)
        if cred is not None:
            new_ref = await secure.put(
                ref=env_var_ref(row.project_id, new_name), value=cred.value,
                metadata={"project_id": str(row.project_id) if row.project_id else None,
                          "name": new_name})
            await secure.delete(old_ref)
            row.credential_ref = new_ref
        else:
            logger.warning("env var %s rename: secret missing at ref %s — "
                           "ref updated, run init will fail fast",
                           row.id, old_ref)
            row.credential_ref = env_var_ref(row.project_id, new_name)
    row.name = new_name


# ── Project endpoints ────────────────────────────────────────────────────────


@router.get("")
async def list_environment_variables(
    project_id: str,
    db: AsyncSession = Depends(get_session),
    _member: tuple[User, Identity] = Depends(require_project_member),
) -> list[EnvVarResponse]:
    """Merged view: platform rows first (read-only here), then project rows.
    Override flags mark name shadowing in both directions."""
    platform_rows = (
        (await db.execute(
            select(EnvironmentVariable)
            .where(EnvironmentVariable.scope == "platform")
            .order_by(EnvironmentVariable.name)))
        .scalars().all())
    project_rows = (
        (await db.execute(
            select(EnvironmentVariable)
            .where(EnvironmentVariable.scope == "project",
                   EnvironmentVariable.project_id == project_id)
            .order_by(EnvironmentVariable.name)))
        .scalars().all())

    platform_names = {r.name for r in platform_rows}
    project_names = {r.name for r in project_rows}
    out = [_row_to_response(r, source="platform",
                            overridden=r.name in project_names)
           for r in platform_rows]
    out += [_row_to_response(r, source="project",
                             overrides_platform=r.name in platform_names)
            for r in project_rows]
    return out


@router.post("", status_code=201)
async def create_environment_variable(
    project_id: str,
    body: EnvVarCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> EnvVarResponse:
    """Create a project-scoped variable. A secret's value goes to
    SecureStorage immediately; only the ref is persisted. Same-named platform
    vars are deliberately allowed — the project row overrides them."""
    name = _validate_name(body.name)
    await _check_duplicate(db, project_id, name)

    if body.value_type == "plain":
        if not body.value:
            raise HTTPException(400, "plain variables require a value")
        _validate_value(name, body.value)
        row = EnvironmentVariable(
            project_id=project_id, scope="project", name=name,
            value_type="plain", value=body.value,
            description=body.description)
    else:
        if not body.value:
            raise HTTPException(400, "secret variables require a value")
        ref = await _store_secret(request, project_id, name, body.value)
        row = EnvironmentVariable(
            project_id=project_id, scope="project", name=name,
            value_type="secret", credential_ref=ref,
            description=body.description)

    db.add(row)
    await _commit_or_conflict(db, name)
    await db.refresh(row)
    logger.info("Environment variable created: project=%s name=%s type=%s",
                project_id, name, body.value_type)
    return _row_to_response(row, source="project")


@router.patch("/{envvar_id}")
async def update_environment_variable(
    project_id: str,
    envvar_id: str,
    body: EnvVarUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
) -> EnvVarResponse:
    """Update a project-scoped variable: rename (secret → ref migration),
    replace/rotate the value, or edit the description. value_type is
    immutable — convert by delete + re-create."""
    row = await _get_envvar_or_404(db, envvar_id, project_id=project_id)

    if "name" in body.model_fields_set and body.name != row.name:
        await _rename_row(request, db, row, _validate_name(body.name))

    if "value" in body.model_fields_set and body.value is not None:
        _validate_value(row.name, body.value)
        if row.value_type == "secret":
            new_ref = await _store_secret(request, project_id, row.name,
                                          body.value)
            row.credential_ref = new_ref
            logger.info("Environment variable secret rotated: %s", envvar_id)
        else:
            row.value = body.value

    if "description" in body.model_fields_set:
        row.description = body.description

    await db.commit()
    await db.refresh(row)
    return _row_to_response(row, source="project")


@router.delete("/{envvar_id}")
async def delete_environment_variable(
    project_id: str,
    envvar_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _pm: tuple[User, Identity] = Depends(require_project_manager),
):
    """Delete a project-scoped variable and its stored secret (if any)."""
    row = await _get_envvar_or_404(db, envvar_id, project_id=project_id)

    if row.credential_ref:
        secure = _secure_storage(request)
        await secure.delete(row.credential_ref)

    await db.delete(row)
    await db.commit()
    logger.info("Environment variable deleted: project=%s name=%s",
                project_id, row.name)
    return Response(status_code=204)


# ── Admin endpoints (platform scope) ─────────────────────────────────────────


@admin_router.get("")
async def list_platform_environment_variables(
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> list[EnvVarResponse]:
    """All platform-scoped variables (apply to every run)."""
    rows = (
        (await db.execute(
            select(EnvironmentVariable)
            .where(EnvironmentVariable.scope == "platform")
            .order_by(EnvironmentVariable.name)))
        .scalars().all())
    return [_row_to_response(r, source="platform") for r in rows]


@admin_router.post("", status_code=201)
async def create_platform_environment_variable(
    body: EnvVarCreate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> EnvVarResponse:
    """Create a platform-scoped variable (project_id NULL)."""
    name = _validate_name(body.name)
    await _check_duplicate(db, None, name)

    if body.value_type == "plain":
        if not body.value:
            raise HTTPException(400, "plain variables require a value")
        _validate_value(name, body.value)
        row = EnvironmentVariable(
            project_id=None, scope="platform", name=name,
            value_type="plain", value=body.value,
            description=body.description)
    else:
        if not body.value:
            raise HTTPException(400, "secret variables require a value")
        ref = await _store_secret(request, None, name, body.value)
        row = EnvironmentVariable(
            project_id=None, scope="platform", name=name,
            value_type="secret", credential_ref=ref,
            description=body.description)

    db.add(row)
    await _commit_or_conflict(db, name)
    await db.refresh(row)
    logger.info("Platform environment variable created: name=%s type=%s",
                name, body.value_type)
    return _row_to_response(row, source="platform")


@admin_router.patch("/{envvar_id}")
async def update_platform_environment_variable(
    envvar_id: str,
    body: EnvVarUpdate,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
) -> EnvVarResponse:
    """Update a platform-scoped variable (same semantics as the project
    PATCH: rename with ref migration, value replace/rotate, description)."""
    row = await _get_envvar_or_404(db, envvar_id, scope="platform")

    if "name" in body.model_fields_set and body.name != row.name:
        await _rename_row(request, db, row, _validate_name(body.name))

    if "value" in body.model_fields_set and body.value is not None:
        _validate_value(row.name, body.value)
        if row.value_type == "secret":
            new_ref = await _store_secret(request, None, row.name, body.value)
            row.credential_ref = new_ref
            logger.info("Platform environment variable secret rotated: %s",
                        envvar_id)
        else:
            row.value = body.value

    if "description" in body.model_fields_set:
        row.description = body.description

    await db.commit()
    await db.refresh(row)
    return _row_to_response(row, source="platform")


@admin_router.delete("/{envvar_id}")
async def delete_platform_environment_variable(
    envvar_id: str,
    request: Request,
    db: AsyncSession = Depends(get_session),
    _admin: tuple[User, Identity] = Depends(require_admin),
):
    """Delete a platform-scoped variable and its stored secret (if any)."""
    row = await _get_envvar_or_404(db, envvar_id, scope="platform")

    if row.credential_ref:
        secure = _secure_storage(request)
        await secure.delete(row.credential_ref)

    await db.delete(row)
    await db.commit()
    logger.info("Platform environment variable deleted: name=%s", row.name)
    return Response(status_code=204)
