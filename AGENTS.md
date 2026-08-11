# AGENTS.md — BheemBhai MVP

Orientation file for AI coding agents working in this repository.

## Project overview

BheemBhai is a governed, containerized pipeline for product-development skills. Two services (Platform API + Engine Service) share a Postgres database. The Engine runs a work-queue consumer loop (ADR-003) and launches Fargate tasks. Both services share the `shared/` package for models, protocols, and providers.

## Stack

- **Backend**: Python 3.12, FastAPI 0.115.x, SQLAlchemy 2.0 (async), Alembic 1.14.x
- **Frontend**: Server-rendered HTML (Jinja2), EduAdmin Bootstrap-5 theme, Alpine.js 3.14
- **Database**: PostgreSQL 16 (via asyncpg)
- **Test**: pytest + pytest-asyncio (unit/integration), Playwright (E2E), FakeRuntime (engine)
- **Infra**: Docker Compose (local), ECS Fargate (prod agent containers)

## Project Layout

| Artifact | Path | Notes |
|----------|------|-------|
| Architecture | `docs/architecture.md` | Container, deployment, sequence diagrams |
| Data model | `docs/data-model.md` | 10 tables, ER diagram, indexes, JSONB shapes |
| Tech stack | `docs/tech-stack.md` | Pinned versions with justification |
| Testing strategy | `docs/testing-strategy.md` | TDD flow, test layers, conventions |
| UI conventions | `docs/ui-conventions.md` | (if created) Design tokens, component patterns |
| ADRs | `docs/adr/ADR-NNN-*.md` | One per significant locked decision |
| API contracts | `docs/api-contracts/*.openapi.yaml` | Platform API + Engine Service specs |
| PRD | `docs/product/PRD.md` | Product requirements document |
| Epics | `docs/product/epics.md` | Epic list |
| Epic map | `docs/product/epic-map.json` | Epic dependency graph |
| Per-epic artifacts | `docs/product/epics/<KEY>/_epic/` | stories.md, story-map.json, epic-sequence.* |
| Per-story artifacts | `docs/product/epics/<KEY>/stories/<STORY_KEY>/` | story-design.md, test-plan.md, verification.md, code-review.md, design-sync.md |
| Design history | `docs/design-history/` | tech-design-proposal.md |
| Source — Platform API | `platform_api/` | FastAPI app with Jinja2 templates |
| Source — Engine Service | `engine_service/` | FastAPI app with worker loop |
| Source — Shared | `shared/bheembhai/` | Models, protocols, providers, config |
| Source — Agent | `agent/` | Dockerfile, run_skill.sh, skills |
| Config | `config/` | Workflow + policy YAML + model profiles |
| Tests — Unit | `tests/unit/<module>/` | Mirrors source tree |
| Tests — Integration | `tests/integration/<module>/` | Mirrors source tree |
| Tests — E2E | `tests/e2e/` | Playwright browser tests |

## Module map

| Source module | Purpose |
|---------------|---------|
| `shared/bheembhai/models/` | SQLAlchemy ORM: User, Project, ProjectRole, Membership, ProjectIntegration, Workflow, Policy, Run, Step, Transition, WorkQueueItem |
| `shared/bheembhai/protocols/` | Pluggable boundaries: AuthProvider, ObjectStorage, SecureStorage |
| `shared/bheembhai/providers/` | Concrete implementations: CognitoProvider, S3Storage, LocalStorage, AWSSecretsManager, EnvSecureStorage |
| `shared/bheembhai/config.py` | AppConfig with env-var loading |
| `shared/bheembhai/database.py` | Async engine + session factory |
| `platform_api/routers/` | health, projects, workflows, runs |
| `engine_service/worker.py` | work_queue claim loop (SKIP LOCKED) |
| `engine_service/recovery.py` | Stale-claim re-enqueue on restart |

## Coding conventions

- **Python**: SQLAlchemy 2.0 Mapped-style models, async throughout, Pydantic v2 for request/response schemas
- **Tests**: Mirror source tree under `tests/<layer>/<module>/`, `test_` prefix, descriptive function names
- **Migrations**: Alembic in `shared/alembic/`, async runner, autogenerate from models

## CI

GitHub Actions: lint (ruff) + unit tests + integration tests on every PR. See `.github/workflows/ci.yml`.
