"""Auth routes — login, refresh, logout, and login page.

Login flow (AWS SDK, no redirect):
  GET  /login              → serve custom login page
  POST /api/auth/login     → email + password → validate via boto3 → return tokens
  POST /api/auth/refresh   → refresh_token → new tokens
  POST /api/auth/logout    → clear session cookie
"""

import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from bheembhai.protocols.auth import Identity
from bheembhai.providers.cognito_auth import AuthError, AuthTokens, CognitoAuthService

from platform_api.dependencies import get_current_user

router = APIRouter(tags=["auth"])
_executor = ThreadPoolExecutor(max_workers=4)

# ── Helpers ───────────────────────────────────────────────────


def _auth_service(request: Request) -> CognitoAuthService | None:
    """Get the Cognito auth service from app state (may be None in dev mode)."""
    return getattr(request.app.state, "cognito_auth_service", None)


def _make_token_response(tokens: AuthTokens) -> JSONResponse:
    """Build a JSON response with tokens in body + id_token in httpOnly cookie."""
    response = JSONResponse(
        content={
            "id_token": tokens.id_token,
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_in": tokens.expires_in,
        }
    )
    response.set_cookie(
        key="bb_id_token",
        value=tokens.id_token,
        httponly=True,
        secure=False,     # set True behind TLS
        samesite="lax",
        max_age=tokens.expires_in,
    )
    return response


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": code, "message": message},
    )


# ── GET /login ────────────────────────────────────────────────


@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Serve the custom login page."""
    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="platform_api/templates")
    return templates.TemplateResponse("login.html", {"request": request})


# ── POST /api/auth/login ─────────────────────────────────────


@router.post("/api/auth/login")
async def login(request: Request) -> JSONResponse:
    """Authenticate email + password against Cognito via boto3.

    Body: {"email": "...", "password": "..."}
    Returns: id_token, access_token, refresh_token, expires_in
    Sets:   httpOnly cookie "bb_id_token"
    """
    body = await request.json()
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""

    if not email or not password:
        return _error_response(400, "MissingFields", "Email and password are required.")

    # ── Dev bypass ────────────────────────────────────────────
    if os.getenv("DEV_AUTH_BYPASS", "").lower() == "true":
        return _make_token_response(
            AuthTokens(
                id_token="dev-id-token",
                access_token="dev-access-token",
                refresh_token="dev-refresh-token",
                expires_in=3600,
            )
        )

    # ── Production: Cognito via boto3 ─────────────────────────
    service = _auth_service(request)
    if service is None:
        return _error_response(500, "NotConfigured", "Auth service not configured.")

    # boto3 is sync — run in thread pool
    result = await _executor_run(service.login, email, password)
    # ^ result is sync but we're calling it from async context

    if isinstance(result, AuthError):
        status = 401 if result.code == "InvalidCredentials" else 400
        return _error_response(status, result.code, result.message)

    return _make_token_response(result)


# ── POST /api/auth/refresh ────────────────────────────────────


@router.post("/api/auth/refresh")
async def refresh_token(request: Request) -> JSONResponse:
    """Exchange a refresh token for new tokens.

    Body: {"refresh_token": "..."}
    """
    body = await request.json()
    refresh_token_val = (body.get("refresh_token") or "").strip()

    if not refresh_token_val:
        return _error_response(400, "MissingFields", "Refresh token is required.")

    # Dev bypass
    if os.getenv("DEV_AUTH_BYPASS", "").lower() == "true":
        return _make_token_response(
            AuthTokens(
                id_token="dev-id-token-refreshed",
                access_token="dev-access-token-refreshed",
                refresh_token="dev-refresh-token-refreshed",
                expires_in=3600,
            )
        )

    service = _auth_service(request)
    if service is None:
        return _error_response(500, "NotConfigured", "Auth service not configured.")

    result = await _executor_run(service.refresh, refresh_token_val)

    if isinstance(result, AuthError):
        return _error_response(401, result.code, result.message)

    return _make_token_response(result)


# ── POST /api/auth/logout ─────────────────────────────────────


@router.post("/api/auth/logout")
async def logout() -> JSONResponse:
    """Clear the session cookie."""
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie("bb_id_token")
    return response


# ── Thread-pool helper ────────────────────────────────────────


# ── GET /api/auth/me ─────────────────────────────────────────


@router.get("/api/auth/me")
async def whoami(user: Identity | None = Depends(get_current_user)) -> dict:
    """Return the current authenticated user's identity, or 401."""
    if user is None:
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthenticated", "message": "Not logged in."},
        )
    return {
        "external_id": user.external_id,
        "email": user.email,
        "display_name": user.display_name,
        "provider": user.provider,
    }


async def _executor_run(func, *args):
    """Run a sync boto3 call in the default thread-pool executor."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, func, *args)
