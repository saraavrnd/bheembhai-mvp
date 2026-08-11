# Tech Stack — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10

## Backend (Platform API + Engine Service)

| Component | Version | Purpose | Justification |
|-----------|---------|---------|---------------|
| **Python** | 3.12 | Runtime | Current stable; FastAPI fully supports; existing codebase is Python 3 |
| **FastAPI** | 0.115.x | Web framework | Already running; async-native; Pydantic v2 built in; auto OpenAPI docs |
| **Uvicorn** | 0.34.x | ASGI server | FastAPI default; production-grade |
| **SQLAlchemy** | 2.0.x | ORM | Async-native (2.0 style); mature migration story via Alembic; supports both Postgres and SQLite (test) |
| **Alembic** | 1.14.x | Schema migrations | Versioned, reviewable schema changes; critical for shared DB between two services |
| **PyYAML** | 6.x | YAML parsing | Already used for workflow/policy config |
| **boto3** | 1.36.x | AWS SDK | Fargate (ECS), S3 (first ObjectStorage backend), Secrets Manager (first SecureStorage backend) — all AWS interactions. For non-AWS deployments, only the Fargate portions are unused; storage and secrets go through their respective provider abstractions. |
| **PyGithub** | 2.x | GitHub REST API | Verify repo access during project integration setup (agent still uses GitHub MCP) |
| **Jinja2** | 3.1.x | HTML templating | Built into FastAPI; server-rendered UI per ADR-001 |
| **pyjwt[crypto]** | 2.10.x | JWT verification | Token signature + expiry validation in auth provider plugins (ADR-010). Used by Cognito, Azure AD, and Okta providers. |
| **httpx** | 0.28.x | Async HTTP client | JWKS endpoint fetching in auth providers; also used for Engine → Platform API webhooks |
| **azure-storage-blob** | 12.x | Azure Blob Storage SDK | ObjectStorage backend for Azure deployments (ADR-011). Only imported when `storage_backend: azure_blob`. |
| **azure-keyvault-secrets** | 4.x | Azure Key Vault SDK | SecureStorage backend for Azure deployments (ADR-012). Only imported when `secure_storage_backend: azure_key_vault`. |
| **hvac** | 2.x | HashiCorp Vault client | SecureStorage backend for on-prem deployments (ADR-012). Only imported when `secure_storage_backend: hashicorp_vault`. |
| **minio** | 7.x | MinIO client library | ObjectStorage backend for on-prem / S3-compatible deployments (ADR-011). Only imported when `storage_backend: minio`. |
| **python-json-logger** | 3.x | Structured logging | JSON logs → CloudWatch Logs |

## Frontend (server-rendered HTML)

| Component | Version | Purpose | Justification |
|-----------|---------|---------|---------------|
| **EduAdmin theme** | Purchased (ThemeForest) | Bootstrap-5 admin UI kit | Production admin template — tables, forms, modals, cards, widgets, auth pages. Vendored unmodified at `app/static/vendor/eduadmin/`. ADR-001. |
| **Bootstrap** | 5.3.x | CSS framework | Vendored with theme. Grid, utilities, components as the baseline. |
| **Alpine.js** | 3.14.x | Client interactivity | Lightweight (no build step). Form state, toggles, polling timers, gate actions. Vendored at `app/static/vendor/alpine/`. |
| **Mermaid.js** | 11.x | Client-side diagrams | Architecture/state diagrams in UI. Vendored. |
| **jQuery** | 3.7.1 | Theme chrome only | Vendored with EduAdmin theme (sidebar, push-menu, treeview, dropdowns). Never write app code against it (ADR-019 from Learn Portal carries forward). |

## Infrastructure (AWS)

| Component | Purpose |
|-----------|---------|
| **Cognito User Pool** | User authentication, JWT issuance |
| **ALB** | TLS termination, Cognito auth action, JWT validation at edge, routing to Platform API |
| **EC2** | Platform API + Engine Service host (systemd-managed) |
| **ECR** | Agent Docker image registry |
| **ECS Fargate** | Agent container execution — one task per step |
| **RDS PostgreSQL 16** | Persistent state — multi-AZ for HA |
| **S3** | Execution artifacts (result JSON, logs, diagnostics) — first ObjectStorage backend (ADR-011). Pluggable protocol supports Azure Blob, MinIO, local FS. Lifecycle policy: expire artifacts after 90 days. |
| **Secrets Manager** | Per-project GitHub/Jira tokens — first SecureStorage backend (ADR-012). Pluggable protocol supports Azure Key Vault, HashiCorp Vault, encrypted env. |
| **CloudWatch Logs** | Structured log aggregation from both services |

## Agent container

| Component | Version | Purpose |
|-----------|---------|---------|
| **Node.js** | 20-slim | Base image |
| **Claude Code CLI** | latest (`@anthropic-ai/claude-code`) | Skill execution |
| **git** | latest apt | Clone, commit, push |
| **jq** | latest apt | JSON construction, result parsing |
| **Python 3** | latest apt | boto3/S3 result upload (or awscli) |
| **uv / uvx** | latest | Atlassian MCP (`uvx mcp-atlassian`) |

## Local development

| Component | Purpose |
|-----------|---------|
| **docker-compose** | Local dev environment: Platform API + Engine Service + Postgres + LocalStack (Secrets Manager mock for SecureStorage) |
| **SQLite** | Test-only — `test_engine.py` FakeRuntime tests use in-memory SQLite |
| **LocalStorage** | Dev-only ObjectStorage backend (ADR-011) — writes to local filesystem, no external service needed. Replaces LocalStack S3 for artifact testing. |

## Versions explicitly NOT used

| Component | Why not |
|-----------|---------|
| React / Vue / SPA framework | ADR-001 — server-rendered HTML with EduAdmin theme |
| Redis / ElastiCache | ADR-003 — Engine Service is the worker, no queue needed |
| SQS / Step Functions | ADR-003 — Engine's state machine is the workflow loop |
| Celery / ARQ | ADR-003 — asyncio tasks in the Engine process, no external queue |
| DynamoDB | ADR-004 — relational query patterns; Postgres is the right fit |
| EFS | ADR-005 — S3 for artifacts is simpler and cheaper |
| Kubernetes / EKS | ADR-002 — two services on EC2, not a cluster workload |
