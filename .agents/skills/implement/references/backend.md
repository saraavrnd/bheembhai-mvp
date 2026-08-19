# Backend Lens — building API/service stories well

The default `implement` path. Loaded (implicitly) when `story-design` tags a story **backend** or
**full-stack**. Carries the API/service craft so backend work is consistent.

## Read the project's contracts and data model first
- `docs/api-contracts/` is the SOURCE OF TRUTH for endpoint shapes — implement to the contract,
  don't invent a different request/response.
- `docs/data-model.md` for entities/fields the story touches.
- The story-design note for the target module and interfaces.

## Backend craft
- **Validation at the boundary** — validate/parse all external input; never trust the caller.
- **Errors** — return the correct status codes and messages the acceptance criteria specify;
  don't leak internals in error bodies.
- **Persistence** — use the project's data layer/ORM pattern; keep writes transactional where a
  failure mid-operation would leave inconsistent state.
- **Security NFRs** — apply the auth/RBAC, secret-handling, and PII rules from `tech-design`'s
  NFRs (the code-review security audit will check these).
- **Config** — via the project's settings/env mechanism, not hard-coded.
- **Idempotency/concurrency** where the contract or NFRs require it.

## Tests for backend (with test-creator)
- Unit tests for logic/validation; integration tests for the endpoint + persistence round-trip.
- Cover the unhappy paths the acceptance criteria specify (bad input, unauthorized, not-found,
  conflict), not just the happy path.
- Place per the test layout (`tests/unit/<module>/`, `tests/integration/<module>/`).

## Full-stack stories
Build the backend slice here, then the UI slice via `references/frontend.md`. The UI must consume
the real contract from `docs/api-contracts/`; integration/e2e tests prove the halves meet.
