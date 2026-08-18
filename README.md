# BheemBhai MVP

A governed, containerized pipeline for product-development skills — a backend that orchestrates a library of AI-powered skills as an assembly line, with human-in-the-loop gates where policy demands approval.

## Architecture

Two services + a shared package + an agent container:

| Component | Purpose | Port |
|-----------|---------|------|
| **Platform API** | User-facing REST API + server-rendered HTML UI | 9000 |
| **Engine Service** | Internal state machine, Fargate lifecycle, work queue consumer | 9001 |
| **Shared package** | SQLAlchemy models, provider protocols (Auth, Storage, Secrets), Alembic migrations | — |
| **Agent container** | Docker image running Claude Code CLI for skill execution (existing) | — |

**Key design principles:**

- Backend owns orchestration; the skill's `next` hint is advisory, the workflow is authoritative.
- One ephemeral container per step/attempt — isolation first.
- Two signals on separate channels: the result payload (from the container) and the exit
  status (polled from outside). A reconciler joins them.
- Three overlays kept separate: **workflow** (what runs + routing), **policy** (which steps
  gate on a human), **notifications** (who's told).
- **Pluggable providers** — Auth (ADR-010), Storage (ADR-011), Secrets (ADR-012) are Protocol-based, config-driven.
- **Postgres-backed work queue** — `SKIP LOCKED` pull model with claim+heartbeat crash recovery (ADR-003).

## Quick start

```bash
# 1. Clone and set up environment
cp .env.example .env
# Edit .env with your API keys

# 2. Build the agent image
docker build -t bheembhai/agent:latest agent/

# 3. Start the stack
docker-compose up -d

# 4. Verify
curl http://localhost:9000/health       # Platform API
curl http://localhost:9001/engine/health # Engine Service

# 5. Run tests
pip install -e shared/[dev]
pytest tests/unit/ -v
pytest tests/integration/ -v
```

## Project layout

```
bheembhai-mvp/
├── platform_api/        # FastAPI app — user-facing REST API + HTML UI
│   ├── routers/         # health, projects, workflows, runs
│   ├── templates/       # Jinja2 server-rendered HTML
│   └── static/          # EduAdmin theme, Alpine.js, Mermaid.js
├── engine_service/      # FastAPI app — internal state machine
│   ├── routers/         # health, webhooks
│   ├── worker.py        # work_queue consumer (SKIP LOCKED)
│   └── recovery.py      # crash recovery on restart
├── shared/              # Shared Python package
│   ├── bheembhai/
│   │   ├── models/      # SQLAlchemy ORM (10 tables)
│   │   ├── protocols/   # AuthProvider, ObjectStorage, SecureStorage
│   │   └── providers/   # Cognito, S3, LocalStorage, AWSSecrets, EnvSecrets
│   └── alembic/         # Database migrations
├── agent/               # Agent container (Dockerfile + run_skill.sh) — existing
├── config/              # Workflow and policy YAML configs — existing
├── tests/
│   ├── unit/            # Fast, no I/O (shared/, platform_api/, engine_service/)
│   ├── integration/     # Real Postgres + HTTP contracts
│   └── e2e/             # Playwright browser tests
└── docs/                # Design artifacts (ADRs, data model, API contracts)
```

## Running tests

```bash
# Unit tests only (fast — no database)
pytest tests/unit/ -v

# Integration tests (needs Postgres)
DATABASE_URL=postgresql+asyncpg://bheembhai:bheembhai@localhost:5555/bheembhai_test \
  pytest tests/integration/ -v

# End-to-end tests (needs running stack)
pytest tests/e2e/ -v

# Existing engine tests (FakeRuntime, no Docker)
python3 test_engine.py
```

## Configuration

All configuration is via environment variables. See `.env.example` for the full list.

### Core

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | Postgres connection string |
| `SECRET_KEY` | — | Session/CSRF secret |
| `ANTHROPIC_API_KEY` | — | Anthropic model credential |

### Providers (ADR-010, ADR-011, ADR-012)

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_PROVIDER` | `cognito` | Auth provider: `cognito`, `azure_ad`, `okta` |
| `STORAGE_BACKEND` | `local` | Object storage: `s3`, `azure_blob`, `minio`, `local` |
| `SECURE_STORAGE_BACKEND` | `env` | Secure storage: `aws_secrets_manager`, `azure_key_vault`, `hashicorp_vault`, `env` |

### Engine

| Variable | Default | Purpose |
|---|---|---|
| `ENGINE_ID` | `engine-1` | Unique ID per Engine instance |
| `BB_HEARTBEAT_INTERVAL` | `30` | Seconds between heartbeat updates |
| `BB_STALE_HEARTBEAT_THRESHOLD` | `60` | Seconds before a claim is considered stale |
| `BB_POLL_INTERVAL` | `5` | Seconds between work queue polls |

## Documentation

- [Architecture](docs/architecture.md)
- [Data Model](docs/data-model.md)
- [Tech Stack](docs/tech-stack.md)
- [Testing Strategy](docs/testing-strategy.md)
- [ADRs](docs/adr/)
- [API Contracts](docs/api-contracts/)

## License

Proprietary — internal tool.
