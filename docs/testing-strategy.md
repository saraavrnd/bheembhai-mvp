# Testing Strategy — BheemBhai MVP

**Status:** Approved · **Date:** 2026-08-10

## Philosophy

**TDD (Test-Driven Development).** Acceptance criteria become failing tests before
implementation. This is what `test-creator` (write tests from story acceptance criteria) and
`test-verify` (verify tests pass — honest green check) rely on downstream. Every story's
definition of done includes passing tests that prove the acceptance criteria are met.

## Test layers

### Unit tests

| Layer | Runner | Scope | Examples |
|-------|--------|-------|----------|
| Engine logic | `pytest` | Pure functions, no I/O | Policy evaluation (`validate_pairing`), result reconciliation, workflow routing, failure classification |
| Platform services | `pytest` | Business logic with mocked DB | User service, project service, workflow validation |
| Models | `pytest` | SQLAlchemy model validation | Field constraints, JSONB shapes, FK integrity |

**Key principle:** Engine unit tests use the existing `FakeRuntime` — no containers, no AWS,
no network. The 9 existing tests in `test_engine.py` are the pattern and carry forward.

### Integration tests

| Layer | Runner | Scope | Examples |
|-------|--------|-------|----------|
| API endpoints | `pytest` + `httpx.AsyncClient` (FastAPI TestClient) | Full request → response against test Postgres | Create project → create workflow → create policy (tied to workflow) → start run → poll for events |
| Engine service | `pytest` + TestClient | Engine HTTP endpoints with FakeRuntime | POST /engine/runs → verify state transitions → POST /engine/runs/{id}/continue |
| Service-to-service | `pytest` | Platform API → Engine Service HTTP contract | Start run (Platform → Engine), approval flow (Platform → Engine → webhook → Platform) |
| Database migrations | `pytest` + Alembic | Migration up → verify schema → migration down | Every migration is tested in both directions |

**Key principle:** Integration tests run against a real Postgres (test database, created and
destroyed per session) and use the actual HTTP contracts between services. `FakeRuntime`
stands in for Fargate — it writes result JSON files and returns exit codes, same as the
current test pattern.

### End-to-end tests

| Layer | Runner | Scope | Examples |
|-------|--------|-------|----------|
| Critical user journeys | Playwright | Real browser against a running instance | Login (Cognito-hosted UI) → create project → configure GitHub integration → create workflow → create policy → start run → see run progress through steps → approve at gate → view completed run history |
| Approval flow | Playwright | Full gate card interaction | Run hits gate → reviewer opens UI → sees gate card with file list → opens a file → approves → run continues |
| Error paths | Playwright | Failure scenarios | Invalid workflow YAML → save-time validation error. Fargate task crashes → retry → escalation |

**Key principle:** E2E tests cover the happy path through the full system. They are fewer
(slower, more brittle) but prove the critical integration: Cognito → ALB → Platform API →
Engine → Fargate → S3 → result visible in UI.

## Test infrastructure

| Component | Local dev | CI |
|-----------|-----------|-----|
| Postgres | Docker (`docker-compose`) | GitHub Actions service container |
| S3 | LocalStack | LocalStack in CI or test bucket in AWS |
| Fargate | FakeRuntime (no real Fargate) | FakeRuntime |
| Cognito | Mock JWT (test-only signing key) | Mock JWT |
| Browser (E2E) | Playwright (headed) | Playwright (headless) |

## What's NOT tested (by design)

- **Claude Code output quality** — the agent container's actual AI output is not tested
  deterministically. The engine tests verify that given a result status (from FakeRuntime),
  the engine routes correctly. The agent's `run_skill.sh` is tested structurally (diagnostics
  written, result parsed) but not the LLM's output.
- **Fargate task launch** — `boto3.run_task()` is mocked in integration tests. Real Fargate
  behavior is verified in a staging environment, not in CI.
- **Cognito OAuth flow** — the hosted UI login redirect is not tested in CI. E2E tests in
  staging use a test user.

## Test conventions

- **File location:** Unit tests co-located with source (`_test.py` suffix or `tests/` subdir).
  Integration tests in `tests/integration/`. E2E tests in `tests/e2e/`.
- **Naming:** `test_<module>_<scenario>.py`. Descriptive test function names:
  `test_policy_save_rejects_gate_on_unknown_step`.
- **Fixtures:** `conftest.py` at each test level provides: test DB session, test client,
  FakeRuntime, mock JWT user.
- **Coverage target:** 80%+ on engine logic and platform services. E2E is not coverage-measured.
- **Run command:**
  ```bash
  pytest tests/ -v                    # unit + integration
  pytest tests/e2e/ --browser chromium # e2e
  ```

## Traceability

| Story phase | Testing role |
|-------------|--------------|
| `test-creator` | Writes unit + integration tests from story acceptance criteria BEFORE implementation |
| `implement` | Makes those tests pass (TDD red → green) |
| `test-verify` | Runs the full suite; reports BLOCK if any test fails (honest-green check) |
| `code-review` | Reviews tests as part of the review — are the right things tested? Are edge cases covered? |

## Relationship to existing tests

The 9 tests in `test_engine.py` carry forward as engine unit tests. They are the model:
`FakeRuntime`, deterministic scripts, verification of routing, context injection purity,
and failure classification. New engine tests follow the same pattern.
