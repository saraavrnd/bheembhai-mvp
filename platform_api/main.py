"""Platform API — user-facing FastAPI application."""

import logging
import os
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Configure logging so app logs are visible in Docker output
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
# Also enable uvicorn access logs
logging.getLogger("uvicorn.access").setLevel(logging.INFO)
logging.getLogger("uvicorn").setLevel(logging.DEBUG)

from bheembhai.config import load_config
from bheembhai.database import close_database, init_database, run_migrations, seed_default_roles, seed_default_skills, seed_default_workflows
from bheembhai.providers.aws_secrets import AWSSecretsManager
from bheembhai.providers.aws_ssm import AWSSSMParameterStore
from bheembhai.providers.cognito import CognitoProvider
from bheembhai.providers.cognito_auth import CognitoAuthService
from bheembhai.providers.env_secrets import EnvSecureStorage

from platform_api.routers import (
    admin,
    auth,
    health,
    integrations,
    policies,
    projects,
    refdata,
    runs,
    workflows,
)

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
    await seed_default_skills()
    await seed_default_workflows()

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


# ── Request-logging middleware ─────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed_ms = (time.time() - start) * 1000
    # Use print so it definitely appears in Docker logs
    print(f"  REQ  {request.method} {request.url.path} → {response.status_code} ({elapsed_ms:.0f} ms)", flush=True)
    return response


@app.get("/")
async def root(request: Request):
    """Root page — user dashboard with project selector."""
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})


@app.get("/dashboard")
async def dashboard(request: Request):
    """User dashboard — project selector landing page."""
    return templates.TemplateResponse(request, "dashboard.html", {"request": request})


@app.get("/projects/{project_id}", include_in_schema=False)
async def project_page(project_id: str, request: Request):
    """User project workspace — dashboard, runs and configuration tabs."""
    return templates.TemplateResponse(
        request, "project.html", {"request": request, "project_id": project_id}
    )


@app.get("/projects/{project_id}/runs/{run_id}", include_in_schema=False)
async def run_detail_page(project_id: str, run_id: str, request: Request):
    """Run detail — stage rail, output viewer, review gates."""
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"request": request, "project_id": project_id, "run_id": run_id},
    )


@app.get("/projects/{project_id}/config/workflows/{workflow_id}", include_in_schema=False)
async def project_workflow_edit_page(project_id: str, workflow_id: str, request: Request):
    """Project-manager workflow editor — same editor as admin, project-scoped API."""
    return templates.TemplateResponse(
        request, "project_workflow_edit.html",
        {"request": request, "project_id": project_id, "workflow_id": workflow_id},
    )


@app.get("/admin", include_in_schema=False)
async def admin_index(request: Request):
    """Admin dashboard — overview page."""
    return templates.TemplateResponse(request, "admin/dashboard.html", {"request": request})


@app.get("/admin/projects", include_in_schema=False)
async def admin_projects(request: Request):
    """Admin projects management page."""
    return templates.TemplateResponse(request, "admin/projects.html", {"request": request})


@app.get("/admin/projects/{project_id}/members", include_in_schema=False)
async def admin_project_members(project_id: str, request: Request):
    """Admin project members management page."""
    return templates.TemplateResponse(request, "admin/project_members.html", {"request": request})


@app.get("/admin/projects/{project_id}/workflows", include_in_schema=False)
async def admin_project_workflows(project_id: str, request: Request):
    """Admin project workflow mapping page."""
    return templates.TemplateResponse(request, "admin/project_workflows.html", {"request": request})


@app.get("/admin/projects/{project_id}/integrations", include_in_schema=False)
async def admin_project_integrations(project_id: str, request: Request):
    """Admin project integrations management page."""
    return templates.TemplateResponse(request, "admin/project_integrations.html", {"request": request})


@app.get("/admin/users", include_in_schema=False)
async def admin_users(request: Request):
    """Admin users management page."""
    return templates.TemplateResponse(request, "admin/users.html", {"request": request})


@app.get("/admin/skills", include_in_schema=False)
async def admin_skills(request: Request):
    """Admin skills library — list page."""
    return templates.TemplateResponse(request, "admin/skills.html", {"request": request})


@app.get("/admin/skills/{skill_id}", include_in_schema=False)
async def admin_skill_edit(skill_id: str, request: Request):
    """Admin skills library — detail/edit page."""
    return templates.TemplateResponse(
        request, "admin/skill_edit.html", {"request": request, "skill_id": skill_id}
    )


@app.get("/admin/workflows", include_in_schema=False)
async def admin_workflows(request: Request):
    """Admin workflow management — list page."""
    return templates.TemplateResponse(request, "admin/workflows.html", {"request": request})


@app.get("/admin/workflows/{workflow_id}", include_in_schema=False)
async def admin_workflow_edit(workflow_id: str, request: Request):
    """Admin workflow management — detail/edit page."""
    return templates.TemplateResponse(
        request, "admin/workflow_edit.html", {"request": request, "workflow_id": workflow_id}
    )


# Routers
app.include_router(auth.router)
app.include_router(health.router)
app.include_router(admin.router)
app.include_router(integrations.router)
app.include_router(projects.router)
app.include_router(workflows.router)
app.include_router(policies.router)
app.include_router(refdata.router)
app.include_router(runs.router)

# Static files (theme, Alpine.js, Mermaid.js)
app.mount("/static", StaticFiles(directory="platform_api/static"), name="static")
