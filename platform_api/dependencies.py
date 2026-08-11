"""Shared FastAPI dependencies — auth, DB sessions, config."""

import logging
import os

from fastapi import Cookie, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.database import get_session
from bheembhai.models.user import User
from bheembhai.protocols.auth import Identity
from bheembhai.providers.cognito import CognitoProvider

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
        print(f"  AUTH  no token (all headers listed above)", flush=True)

    # ── Production: validate JWT against Cognito ──────────────
    provider: CognitoProvider | None = getattr(
        request.app.state, "cognito_provider", None
    )

    if provider is None:
        print(f"  AUTH  get_current_user: NO CognitoProvider on app.state", flush=True)
        return None

    print(f"  AUTH  get_current_user: validating token (source={source}, len={len(token)})...", flush=True)
    identity = await provider.validate(token)
    if identity is None:
        print(f"  AUTH  get_current_user: validation returned None", flush=True)
        return None

    print(f"  AUTH  get_current_user: authenticated as {identity.display_name} ({identity.email})", flush=True)
    return identity


async def require_admin(
    request: Request,
    identity: Identity | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> tuple[User, Identity]:
    """Require platform ADMIN role — raises 401 if unauthenticated, 403 if not ADMIN.

    Returns the DB User row + Identity so downstream handlers can use both.
    """
    if identity is None:
        raise HTTPException(401, "Authentication required")
    user = await get_or_create_user(identity, db)
    if user.platform_role != "ADMIN":
        raise HTTPException(403, "Admin access required")
    return user, identity
