# Technical Design Proposal — Learn Portal (MVP)

**Status:** AWAITING APPROVAL · **Source:** PRD.md + epics · **Date:** 2026-06-19

> This is a proposal. Nothing is committed until you approve. Decisions needing your input are
> in §7. Change anything; I'll revise and re-confirm before writing the final design.

## 1. System summary
Learn Portal MVP is a chat-based Socratic tutor grounded in teacher-uploaded materials, with a
teacher dashboard and roster import. The dominant technical drivers are: a retrieval pipeline
over uploaded documents, an LLM dialogue loop with strict no-answer behaviour and a verifier,
and K-12 privacy/compliance. Throughput is modest at pilot scale (one or a few schools), so the
design optimises for clarity and safety over horizontal scale.

## 2. MVP vs Later (scope the design)
**Must support now:** document upload + chunking + retrieval, Socratic chat with grounding and
safety classifier, basic teacher dashboard signals, OneRoster/CSV import with idempotency,
local Docker deployment. **Leave room for (not now):** sketch interpretation, math-solver
routing, predictive at-risk automation, cross-school normalisation. The architecture must not
hard-code single-school assumptions but need not implement multi-tenant scaling yet.

## 3. Decisions, with options & recommendation

### 3.1 Language & framework (backend)
| Option | Pros | Cons |
|--------|------|------|
| Python + FastAPI | Best AI/ML ecosystem (embeddings, LLM SDKs, vector clients); fast to build; async | Slightly more ops care for typing discipline |
| Node + NestJS | One language with frontend; strong structure | Weaker ML/embeddings ecosystem; more glue for retrieval |
| Go | Fast, simple deploys | Sparse AI ecosystem; slower to build the LLM/RAG parts |
**Recommendation:** Python + FastAPI — the retrieval/LLM/verifier core is the heart of the
product and Python's ecosystem makes it materially faster and safer to build.

### 3.2 Frontend
| Option | Pros | Cons |
|--------|------|------|
| React + TypeScript + Vite | Mature, accessible component libs; fits chat + dashboard; large talent pool | SPA accessibility needs care |
| SvelteKit | Lean, fast | Smaller ecosystem for a11y-audited components |
**Recommendation:** React + TypeScript + Vite — meets the WCAG AA NFR with well-audited
component libraries and suits both the chat UI and the dashboard.

### 3.3 Architecture style
**Recommendation:** **Modular monolith** (one FastAPI app with clear internal modules:
`ingestion`, `retrieval`, `dialogue`, `safety`, `dashboard`, `sis`). At pilot scale a service
mesh adds ops cost without benefit. Seams: each module has a defined interface, so
`retrieval` or `safety` can be extracted into a service later without a rewrite.

### 3.4 Data stores
**Recommendation:** PostgreSQL as the primary store (users, rosters, sessions, signals —
encryptable at rest, mature RBAC patterns) + ChromaDB as the vector store for retrieval
chunks. Two stores, each doing what it's good at; both run as local containers.

### 3.5 Key libraries / external services
**Recommendation:** an LLM SDK for generation + the embedding model for retrieval, a Mermaid
CLI container for diagram rendering, and an OpenAPI-generated client for the frontend. Nothing
beyond what the MVP requirements demand.

### 3.6 Deployment target
**Recommendation:** local **docker-compose** stack (api, web, postgres, chroma, mermaid
renderer) — honouring your stated target — structured so the same images run on a remote host
or orchestrator later with only compose/manifest changes.

### 3.7 Testing approach
**Recommendation:** **TDD.** Each story's Gherkin acceptance criteria become failing tests
first, then implementation makes them green. Runners: pytest (unit + integration, API), and
Playwright (end-to-end for the chat/dashboard flows). Behavioural AI checks (no-answer
adherence, grounding) get their own pytest-based suite seeded from acceptance criteria.

### 3.8 Repository shape
**Recommendation:** monorepo with `apps/api`, `apps/web`, `packages/contracts` (shared API
types), `infra/` (compose, Dockerfiles), `docs/` (this design + ADRs). One repo keeps the
contract shared and the MVP easy to run.

## 4. Proposed architecture (overview)
Client (React) → API (FastAPI) with modules: ingestion → chunk store; retrieval (Chroma top-k)
+ CAG assets → dialogue (LLM, Socratic prompt) → safety classifier + verifier → response.
Dashboard module reads aggregated session signals from Postgres. SIS module imports rosters
into Postgres. Mermaid renderer is a sidecar container called by the dialogue module.

## 5. Proposed data model (sketch)
`teacher`, `student`, `course`, `material`(→chunks in Chroma), `session`, `message`,
`signal`(aggregated per session), `roster_import`(audit). Traces to FR-004/005/006 (materials),
FR-012 (signals), FR-014/015/016 (roster), NFR-002 (encryptable PII on student/teacher).

## 6. Versions (pinned)
| Component | Version | Notes |
|-----------|---------|-------|
| (to verify at write-time) | | Confirm current stable versions via search before committing |

## 7. Decisions I need from you
- [ ] D1 — Approve Python/FastAPI backend + React/TS frontend?
- [ ] D2 — Approve modular monolith for MVP (vs services now)?
- [ ] D3 — Approve Postgres + ChromaDB as the two stores?
- [ ] D4 — Confirm TDD with pytest + Playwright?

## 8. Assumptions made
Pilot scale (one/few schools); teachers supply curriculum; single deployment per district;
English-first UI for MVP.

---
*After approval, `tech-design` writes architecture.md, ADRs, data-model.md, api-contracts/,
tech-stack.md, and testing-strategy.md, which `project-scaffold` turns into a runnable repo.*
