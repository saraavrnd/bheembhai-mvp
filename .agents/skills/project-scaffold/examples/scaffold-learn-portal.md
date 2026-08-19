# Scaffold — Learn Portal MVP (worked example)

Produced by `project-scaffold` from the approved `tech-design` (Python/FastAPI + React/TS,
modular monolith, Postgres + ChromaDB, docker-compose, TDD with pytest + Playwright).

## Folder structure created (mirrors architecture.md)
```
learn-portal/
├── apps/
│   ├── api/                      # FastAPI modular monolith
│   │   ├── app/
│   │   │   ├── main.py           # boots; /health walking-skeleton endpoint
│   │   │   ├── ingestion/        # stub module (FR-004/005)
│   │   │   ├── retrieval/        # stub module (FR-006)
│   │   │   ├── dialogue/         # stub module (FR-001/002/003)
│   │   │   ├── safety/           # stub module (FR-017, NFR-001)
│   │   │   ├── dashboard/        # stub module (FR-012)
│   │   │   └── sis/              # stub module (FR-014/015/016)
│   │   ├── tests/                            # mirrors app modules; type at top, module under
│   │   │   ├── conftest.py                   # shared fixtures
│   │   │   ├── unit/
│   │   │   │   ├── health/test_health.py     # trivial unit test (passes)
│   │   │   │   ├── ingestion/                # one folder per app module (empty until stories land)
│   │   │   │   ├── retrieval/  dialogue/  safety/  dashboard/  sis/
│   │   │   └── integration/
│   │   │       ├── health/test_health_api.py # trivial integration test (passes)
│   │   │       └── ingestion/  retrieval/  …  # mirror the modules
│   │   ├── pyproject.toml        # deps + ruff + pytest config
│   │   └── Dockerfile
│   └── web/                      # React + TS + Vite
│       ├── src/
│       │   ├── App.tsx           # calls /health, renders status (walking skeleton)
│       │   └── main.tsx
│       ├── e2e/health.spec.ts    # Playwright round-trip test (passes)
│       ├── package.json          # deps + eslint + prettier + playwright
│       └── Dockerfile
├── packages/
│   └── contracts/                # types GENERATED from docs/api-contracts/ at build (not hand-maintained)
├── infra/
│   └── docker-compose.yml        # api, web, postgres, chroma, mermaid renderer
├── docs/                         # artifact tree — single source the skill chain reads/writes
│   ├── architecture.md           data-model.md  tech-stack.md  testing-strategy.md
│   ├── CHANGELOG-design.md        product-overview.md
│   ├── adr/                      # ADR-NNN-*.md (copied from tech-design)
│   ├── api-contracts/            # *.openapi.yaml — interface specs (source of truth)
│   ├── design-history/           # tech-design-proposal.md
│   └── product/                  # PRD.md, epics.md, epic-map.json; epics/<EPIC>/{_epic,stories/<STORY>}/
├── AGENTS.md                     # repo root — living orientation + Project Layout table
├── .github/
│   ├── workflows/ci.yml          # lint + tests on PR
│   └── PULL_REQUEST_TEMPLATE.md
├── .gitignore
├── .env.example
└── README.md
```

> Note: `AGENTS.md` sits at the repo root (agents auto-discover it there) and its **Project
> Layout** table is the single source of truth for the `docs/` paths above. API contract specs
> live in `docs/api-contracts/`; the `packages/contracts/` types are generated from them, not a
> hand-kept second copy.

## Walking skeleton
- `GET /health` → `{"status":"ok"}` (api/app/main.py).
- `web` App.tsx fetches `/health` on load and renders "API: ok".
- `e2e/health.spec.ts` loads the page and asserts it shows "API: ok" — proving frontend →
  API round trip through the container network.

## Verification log (what was actually run)
```
$ make install                # uv sync + npm ci          -> ok
$ make lint                   # ruff + eslint/prettier     -> clean
$ make test                   # pytest + playwright        -> 3 passed (unit, integration, e2e)
$ docker-compose up -d        # api, web, postgres, chroma, mermaid
$ curl localhost:8000/health  # {"status":"ok"}            -> ok
$ open localhost:5173         # shows "API: ok"            -> ok
```
All green. No feature logic implemented — modules are empty stubs.

## Branching & PR conventions installed
- Branch per story: `feat/LEARN-21-upload-course-materials`.
- PR template requires: linked Jira key, acceptance-criteria checklist, test run result.

## Hand-off
Repo is ready for the per-story loop. First story (LEARN-21): `test-creator` writes failing
pytest/Playwright tests from its three Gherkin scenarios, then `implement` fills the
`ingestion` module until green.
