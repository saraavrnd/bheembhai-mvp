# ADR-002: Two-service architecture — Platform API + Engine Service

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

The current BheemBhai is a single FastAPI process (`app.py` + `engine.py`) that serves the UI
AND runs the workflow state machine — launching Docker containers, polling for completion, and
reconciling results — all in-process. This works for a single-user demo on local Docker.

EPIC BEEM-24 requires multi-tenancy (users, projects, per-project credentials) and Fargate-based
container execution. The platform needs to serve multiple users concurrently while the engine
runs long-lived state-machine loops (minutes per run, 7+ steps per run) that manage Fargate
task lifecycles.

The question: keep a modular monolith (platform + engine as packages in one process), or split
them into separate services?

## Decision

**Split into two services: Platform API and Engine Service, communicating via HTTP.**

- **Platform API** (`backend/platform/`): auth, user/project CRUD, workflow/policy CRUD,
  execution history reads, serves the HTML UI, handles approval actions. This is what the
  browser talks to.
- **Engine Service** (`backend/engine/`): workflow state machine, Fargate task lifecycle
  (launch → poll → reconcile → route), policy gate evaluation, event bus. Internal only —
  no direct browser access.

## Alternatives considered

- **Modular monolith (rejected for this scope):** Simpler deploy (one process), no service-to-service
  auth, shared DB access without coordination. But the engine's workload (long-lived async
  state-machine loops) is fundamentally different from the API's workload (short
  request-response). In a monolith, a long engine loop blocks an asyncio thread or requires a
  background task system (ARQ/Celery) — adding operational complexity comparable to a second
  service anyway. Also, independent scaling: the engine's capacity (concurrent Fargate tasks)
  should scale independently of the API's capacity (concurrent browser requests).
- **Full microservices (rejected):** Each concern (auth, projects, workflows, execution) as an
  independent service. Way over-engineered for MVP — operational overhead kills velocity. The
  two-service split respects the natural fault line without fragmentation.

## Consequences

- **Easier:** Independent failure domains. If the Engine restarts, it recovers in-flight runs
  from Postgres; the Platform API stays up for users.
- **Easier:** Engine is testable in isolation — `FakeRuntime` carries forward, and the HTTP
  contract is a clean test boundary.
- **Easier:** Independent scaling. Platform API can run 2+ instances behind ALB; Engine runs as
  one active process (or multiple with advisory locks for HA).
- **Harder:** Service-to-service auth (shared secret header token) and webhook delivery (Engine
  → Platform on gate events). Adds two HTTP surfaces to monitor.
- **Harder:** Shared database between two services means both must agree on schema migrations.
  Mitigated by keeping them in the same repo with a shared `backend/shared/` package.
