# ADR-004: PostgreSQL RDS for persistent state

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

The current BheemBhai uses SQLite at `$BB_WORKDIR/bheembhai.db` — a single file on the EC2
instance. This works for a single-user demo but cannot support:
- Concurrent reads/writes from multiple users (SQLite is single-writer)
- Multi-tenancy (no user isolation at the DB level)
- Two services accessing the same data (Platform API + Engine Service)
- Durability beyond the EC2 instance lifecycle

EPIC BEEM-24 requires multi-user support, per-project data scoping, and shared state between
the Platform API and Engine Service.

## Decision

**PostgreSQL 16 on AWS RDS**, with SQLAlchemy 2.0 as the ORM and Alembic for migrations.

Both services share one RDS instance. The Engine Service is the primary writer for runs/steps/
transitions (execution state). The Platform API is the primary writer for users/projects/
workflows/policies (configuration state). Both read from all tables.

The existing SQLite schema (runs, steps, transitions) migrates directly — the table structure
doesn't change, only the database engine. New tables (users, projects, project_integrations,
workflows, policies) are designed for Postgres from day one, using proper foreign keys,
constraints, and JSONB for flexible fields.

## Alternatives considered

- **SQLite (rejected):** Single-writer, no concurrent access, no network access for two
  services. Cannot support multi-tenancy.
- **DynamoDB (rejected):** Excellent for scale, but the query patterns for BheemBhai are
  relational: runs have many steps, steps have many transitions, projects own workflows, etc.
  DynamoDB would require denormalization and application-level joins — complexity that doesn't
  pay off at MVP scale.
- **Aurora Serverless (rejected for now):** Auto-scaling and pay-per-use are appealing, but
  RDS is simpler to reason about and the cost at MVP scale is predictable. The seam to Aurora
  is thin — same Postgres wire protocol, same SQLAlchemy.

## Consequences

- **Easier:** Multi-tenancy via `WHERE project_id = ?` on every query. Foreign keys enforce
  referential integrity (a run must belong to a valid project). JSONB fields (result payloads,
  workflow YAML) give schema flexibility without losing queryability.
- **Easier:** Alembic migrations give versioned, reviewable schema changes — critical when
  two services share a database.
- **Harder:** Operational overhead vs SQLite — RDS needs provisioning, backup configuration,
  and monitoring (CloudWatch metrics + alarms). This is standard AWS operations, not novel
  complexity.
- **Note:** SQLite stays for `test_engine.py` only — the `FakeRuntime` tests use in-memory
  SQLite for speed and isolation. The database layer is abstracted behind SQLAlchemy so the
  same models work against both backends.
