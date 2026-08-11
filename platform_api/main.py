"""Platform API — user-facing FastAPI application."""

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bheembhai.config import load_config
from bheembhai.database import close_database, init_database, run_migrations, seed_default_roles
from bheembhai.providers.aws_secrets import AWSSecretsManager
from bheembhai.providers.aws_ssm import AWSSSMParameterStore
from bheembhai.providers.cognito import CognitoProvider
from bheembhai.providers.cognito_auth import CognitoAuthService
from bheembhai.providers.env_secrets import EnvSecureStorage

from platform_api.routers import auth, health, integrations, projects, runs, workflows

logger = logging.getLogger(__name__)

templates = Jinja2Templates(directory="platform_api/templates")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: load config, init DB, create tables, wire auth providers."""
    config = load_config()
    app.state.config = config

    init_database(config.database)
    await run_migrations()
    await seed_default_roles()

    # ── Wire Cognito auth service (boto3 — login/refresh/signup) ──
    auth_cfg = config.auth
    if auth_cfg.provider == "cognito" and auth_cfg.cognito_client_id:
        app.state.cognito_auth_service = CognitoAuthService(
            region=auth_cfg.cognito_region,
            user_pool_id=auth_cfg.cognito_user_pool_id,
            client_id=auth_cfg.cognito_client_id,
        )

        app.state.cognito_provider = CognitoProvider(
            region=auth_cfg.cognito_region,
            user_pool_id=auth_cfg.cognito_user_pool_id,
            client_id=auth_cfg.cognito_client_id,
        )

        logger.info(
            "Cognito auth wired: region=%s pool=%s client=%s",
            auth_cfg.cognito_region,
            auth_cfg.cognito_user_pool_id,
            auth_cfg.cognito_client_id,
        )
    else:
        logger.warning("Auth provider not configured — /api/auth/login will return 500")

    # ── Wire SecureStorage provider ────────────────────────────
    secure_cfg = config.secure_storage
    if secure_cfg.backend == "aws_ssm":
        app.state.secure_storage = AWSSSMParameterStore(region=secure_cfg.aws_region)
        logger.info("SecureStorage wired: aws_ssm region=%s", secure_cfg.aws_region)
    elif secure_cfg.backend == "aws_secrets_manager":
        app.state.secure_storage = AWSSecretsManager(region=secure_cfg.aws_region)
        logger.info("SecureStorage wired: aws_secrets_manager region=%s", secure_cfg.aws_region)
    elif secure_cfg.backend == "env":
        app.state.secure_storage = EnvSecureStorage(
            encrypted_config_path=secure_cfg.env_encrypted_config_path
        )
        logger.info("SecureStorage wired: env")
    else:
        logger.warning(
            "SecureStorage backend '%s' not recognised — secrets API will return 500",
            secure_cfg.backend,
        )

    yield

    await close_database()


app = FastAPI(
    title="BheemBhai Platform API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/")
async def root(request: Request):
    """Root page — walking skeleton status page."""
    return templates.TemplateResponse("index.html", {"request": request})


# Routers
app.include_router(auth.router)
app.include_router(health.router)
app.include_router(integrations.router)
app.include_router(projects.router)
app.include_router(workflows.router)
app.include_router(runs.router)

# Static files (theme, Alpine.js, Mermaid.js)
app.mount("/static", StaticFiles(directory="platform_api/static"), name="static")
