---
name: test-creator
description: >
  Write automated tests for a user story from its Gherkin acceptance criteria and approved
  story-design, BEFORE any implementation exists (the TDD red phase). Use this as the second
  step of the per-story delivery loop, after story-design is approved and before implement.
  Trigger this even when the user just says "write the tests", "create tests for LEARN-21",
  "TDD this story", "add the failing tests", or "test the acceptance criteria". This skill
  produces tests that FAIL for the right reason (feature missing, not project broken) and maps
  every acceptance scenario to at least one test. It does NOT implement the feature (implement)
  and does NOT make tests pass.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Uses the project's test runners as defined in
  testing-strategy.md (e.g. pytest, Playwright). Runs against the deployed Docker Compose
  stack for integration and browser tests, while unit tests may remain in-process. Reads the
  story, its story-design note, and the existing test harness from project-scaffold.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# test-creator — Tests First (TDD red phase)

> **Artifact paths:** Resolve all project-artifact locations (architecture, data model, PRD,
> epics, stories, ADRs, API contracts, per-story notes, etc.) from the **Project Layout** table
> in `AGENTS.md` at the repo root — see `_SHARED-project-layout.md`. Do not hardcode paths; the
> current layout places these under `docs/` (e.g. `docs/architecture.md`, `docs/adr/`,
> `docs/api-contracts/`, `docs/product/`). **Per-story artifacts live in that story's own
> folder** `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` with short, un-suffixed
> filenames (story-design.md, test-plan.md, verification.md, code-review.md, design-sync.md);
> epic artifacts in `docs/product/epics/<EPIC_KEY>/_epic/`. Resolve a story's files under its
> folder only. This skill's own `templates/`, `references/`,
> `examples/`, and `scripts/` stay inside the skill folder and never move under `docs/`.

> **Cost & context:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). To avoid context bloat: load only the slices of artifacts you
> need (not whole files), don't re-read what's already in context, and for any MCP calls request
> minimal fields and batch reads rather than fetching repeatedly. MCP results persist for the
> session — keep them small.


Turns a story's acceptance criteria into automated tests that fail because the feature isn't
built yet. This is the red in red-green: the tests become the executable definition of done
that `implement` will satisfy. Because the walking skeleton from `project-scaffold` already
proved the harness runs, a red result here means "feature missing" — not "project
misconfigured."

> Tests are written against the interfaces approved in the story-design note, so they don't get
> rewritten when implementation lands. That alignment is why story-design runs first.

## Input it expects
- The user story with Gherkin acceptance criteria (from `user-story`).
- The approved `story-design.md (in the story folder: docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/)` (interfaces, data deltas, test surface).
- The existing test harness and conventions (from `project-scaffold` / `testing-strategy.md`).

## Output it produces
- Test files placed in the project's test layout — **under the module sub-folder that mirrors the
  source**, one or more tests per acceptance scenario, at the layers named in the design note's
  "Test surface" (see Step 2.5 for placement).
- A short `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md` mapping each Gherkin scenario → the test(s) covering it
  (so coverage is auditable).
- A verification that the new tests **fail for the right reason** when run.

## Procedure

### Step 1 — Gather criteria and interfaces
Read every Gherkin scenario in the story (happy and unhappy paths) and the approved interfaces
from the story-design note. The scenarios define behaviour; the interfaces define what to call.

### Step 2 — Map scenarios to tests (one-to-at-least-one)
For each scenario, plan at least one test at the appropriate layer, guided by the design note's
**domain tag** and test surface:
- Pure logic / validation → unit test.
- Endpoint behaviour, DB effects → integration test (backend / full-stack, preferably against
  the deployed Docker Compose stack).
- UI component render + loading/error/empty/success states + interactions → component test
  (frontend / full-stack).
- User-visible flow → e2e test (Playwright) against the deployed app when the test surface
  calls for it.
Backend stories lean unit+integration; frontend stories lean component+e2e; full-stack spans both.
Record the mapping in `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md`. Every scenario must appear.

### Step 2.5 — Place tests under the mirrored module folder (don't pile them flat)
Tests mirror the source tree, so they stay maintainable as stories multiply. From the
story-design note's **target module**, derive the folder:
- `app/<module>/<file>.py`  →  unit test at `tests/unit/<module>/test_<file>.py`
- integration tests for that module  →  `tests/integration/<module>/test_<thing>.py`
- e2e (user journey)  →  `tests/e2e/test_<journey>.py` (module sub-folder optional)

Rules:
- Put each test in the sub-folder matching the module it exercises — NEVER flat in
  `tests/unit/`. If the module's folder doesn't exist yet, create it (with `__init__.py`/
  `conftest.py` as the runner needs), following the structure `project-scaffold` established.
- One story may touch one module (most common) or a few; place each test by the module it tests,
  not by the story. Tests track code structure, not ticket structure.
- Name test files after the unit under test (`test_validation.py`), not after the story key — the
  test-plan in the story folder is what links tests back to the story for traceability.

### Step 3 — Write the tests against the approved interfaces
Use the project's runner and conventions. Each test:
- Arranges the Given, acts on the When, asserts the Then — concretely (real status codes,
  exact error messages, specific state), matching the acceptance criteria verbatim where they
  specify values.
- Targets the signatures from the story-design note (don't invent a different interface).
- Includes the unhappy paths, not just the happy one. A test suite that only covers the happy
  path is incomplete and must be flagged.
- For stories that create or mutate persisted state, assert the durable source of truth, not a
  temporary shim. If the story-design says the system records a `User`, `Project`, `Run`, or
  similar core entity, at least one test must prove the actual persisted row/state exists in the
  declared store (for example, the database or other approved durable backend), not just a local
  file, in-memory list, or mocked return value.
- For integration and e2e tests, prefer the deployed Docker Compose stack as the system under
  test. Use TestClient or local doubles only for unit tests or for narrow harness setup that the
  story-design explicitly allows.

### Step 4 — Run them and confirm they fail correctly
Run the new tests. They MUST fail, and fail for the intended reason:
- Good red: assertion fails / endpoint 404 / function missing — the feature isn't built.
- Bad red: import error, fixture broken, syntax error, harness not found — that's a test bug,
  fix it. A test that errors instead of failing is not a valid red.
Capture the run output. If a test passes already, the behaviour either exists or the test is
too weak — investigate before continuing.

### Step 5 — Hand off
Report: N tests across the mapped scenarios, all failing for the right reason, coverage of
every acceptance scenario confirmed. Tell the user the next step is `implement`, whose job is
to make exactly these tests pass without weakening them.

## Rules that keep TDD honest
- **Do not implement the feature here.** If you find yourself writing production code to make a
  test pass, stop — that's `implement`.
- **Do not write tests you intend to weaken later.** Tests encode the acceptance criteria; if a
  criterion is wrong, fix the story, not the test.
- **Cover unhappy paths.** Boundary, invalid input, and permission cases are part of done.
- **Pin concrete expectations.** Assert the exact message/status/state the criteria specify, so
  a vague implementation can't pass.
- **Pin the system of record for stateful stories.** A test that only observes a return value or
  a temporary shim is insufficient when the story is about persistent platform state.

## Reference files
- `templates/test-plan.md.tmpl` — the scenario → test mapping.
- `references/tdd-red-phase.md` — what a valid failing test looks like; good vs bad red.
- `examples/` — a test plan + failing tests for story LEARN-21.
