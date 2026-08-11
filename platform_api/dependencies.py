"""Shared FastAPI dependencies — auth, DB sessions, config."""

import logging
import os

from fastapi import Cookie, Depends, Header, Request

from bheembhai.database import get_session
from bheembhai.protocols.auth import Identity
from bheembhai.providers.cognito import CognitoProvider

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
        return Identity(
            external_id="dev-user",
            email="dev@bheembhai.local",
            display_name="Dev User",
            provider="dev",
        )

    # ── Token resolution: header first, then cookie ───────────
    token: str | None = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
    elif bb_id_token:
        token = bb_id_token.strip()

    if not token:
        return None

    # ── Production: validate JWT against Cognito ──────────────
    provider: CognitoProvider | None = getattr(
        request.app.state, "cognito_provider", None
    )

    if provider is None:
        # No provider configured — fall back to dev identity
        return None

    identity = await provider.validate(token)
    if identity is None:
        logger.warning("Token validation failed")
        return None

    return identity
