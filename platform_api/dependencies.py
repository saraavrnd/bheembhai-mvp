"""Shared FastAPI dependencies — auth, DB sessions, config."""

import logging
import os

from bheembhai.database import get_session
from bheembhai.models.user import Membership, User
from bheembhai.protocols.auth import Identity
from bheembhai.providers.cognito import CognitoProvider
from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from platform_api.users import get_or_create_user

logger = logging.getLogger(__name__)


async def get_current_user(
    request: Request,
    authorization: str | None = Header(None),
    bb_id_token: str | None = Cookie(None),
) -> Identity | None:
    """Validate the Bearer token (or cookie) and return the identity.

    In production: validates the JWT against the configured AuthProvider (Cognito).
    In dev (DEV_AUTH_BYPASS=true): returns a hardcoded dev identity — no Cognito needed.

    NOTE: This only validates the JWT — it does NOT check ``is_enabled``.
    Use ``get_current_enabled_user`` or ``require_admin`` for endpoints that need
    the DB user row with an enabled-account gate.
    """
    # ── Dev bypass — skip real auth ───────────────────────────
    if os.getenv("DEV_AUTH_BYPASS", "").lower() == "true":
        logger.debug("get_current_user: DEV_AUTH_BYPASS=true, returning dev identity")
        return Identity(
            external_id="dev-user",
            email="dev@bheembhai.local",
            display_name="Dev User",
            provider="dev",
        )

    # ── Token resolution: header first, then cookie ───────────
    # Dump all request headers for debugging
    for name, value in request.headers.items():
        if name.lower() in ('authorization', 'cookie', 'host'):
            print(f"  AUTH  header: {name}={value[:80] if len(value) > 80 else value}", flush=True)

    token: str | None = None
    source: str = "none"
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        source = "header"
        print(f"  AUTH  token from Authorization header (len={len(token)})", flush=True)
    elif authorization:
        print(f"  AUTH  Authorization header present but NOT Bearer: '{authorization[:50]}'", flush=True)
    elif bb_id_token:
        token = bb_id_token.strip()
        source = "cookie"
        print(f"  AUTH  token from bb_id_token cookie (len={len(token)})", flush=True)
    else:
        print("  AUTH  no token (all headers listed above)", flush=True)

    if token is None:
        return None

    # ── Production: validate JWT against Cognito ──────────────
    provider: CognitoProvider | None = getattr(
        request.app.state, "cognito_provider", None
    )

    if provider is None:
        print("  AUTH  get_current_user: NO CognitoProvider on app.state", flush=True)
        return None

    print(f"  AUTH  get_current_user: validating token (source={source}, len={len(token)})...", flush=True)
    identity = await provider.validate(token)
    if identity is None:
        print("  AUTH  get_current_user: validation returned None", flush=True)
        return None

    print(f"  AUTH  get_current_user: authenticated as {identity.display_name} ({identity.email})", flush=True)
    return identity


async def get_current_enabled_user(
    request: Request,
    identity: Identity | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> tuple[User, Identity] | None:
    """Validate JWT AND check the account is enabled.

    Returns (User, Identity) or None if unauthenticated.
    Raises 403 if the account has been disabled.
    """
    if identity is None:
        return None
    user = await get_or_create_user(identity, db)
    if not user.is_enabled:
        raise HTTPException(403, "Account is disabled. Contact an administrator.")
    return user, identity


async def require_admin(
    request: Request,
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> tuple[User, Identity]:
    """Require platform ADMIN role — raises 401 if unauthenticated, 403 if not ADMIN.

    Returns the DB User row + Identity so downstream handlers can use both.
    Also gates on is_enabled (via get_current_enabled_user).
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    user, identity = enabled
    if user.platform_role != "ADMIN":
        raise HTTPException(403, "Admin access required")
    return user, identity


async def _project_membership(
    project_id: str,
    db: AsyncSession,
    current_user: User,
) -> Membership:
    """Look up the current user's membership in a project.

    Raises 404 if the project doesn't exist, 403 if the user isn't a member.
    """
    from bheembhai.models.project import Project
    from sqlalchemy import select as _select

    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")

    membership = (await db.execute(
        _select(Membership).where(
            Membership.user_id == current_user.id,
            Membership.project_id == project_id,
        )
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(403, "You are not a member of this project")
    return membership


async def require_project_member(
    project_id: str,
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
    db: AsyncSession = Depends(get_session),
) -> tuple[User, Identity]:
    """Require membership in the project named by the ``project_id`` path param.

    401 unauthenticated · 404 unknown project · 403 non-member.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    user, identity = enabled
    await _project_membership(project_id, db, user)
    return user, identity


async def require_project_manager(
    project_id: str,
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
    db: AsyncSession = Depends(get_session),
) -> tuple[User, Identity]:
    """Require the ``project_manager`` role in the project named by ``project_id``.

    401 unauthenticated · 404 unknown project · 403 non-member or wrong role.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    user, identity = enabled
    membership = await _project_membership(project_id, db, user)
    if membership.role != "project_manager":
        raise HTTPException(403, "Only a project manager can do this")
    return user, identity
