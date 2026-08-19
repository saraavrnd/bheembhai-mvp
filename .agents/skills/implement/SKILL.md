---
name: implement
description: >
  Implement a user story by writing the minimum code needed to make its failing tests pass (the
  TDD green phase), on a story branch, following the approved story-design and project
  conventions. Use this as the third step of the per-story delivery loop, after test-creator has
  written failing tests. Trigger this even when the user just says "implement LEARN-21", "make
  the tests pass", "build this story", "write the code", or "green the tests". This skill writes
  production code to satisfy the existing tests without weakening them; it does NOT write the
  tests (test-creator) and does NOT open the PR (pr-create). It works on a branch named for the
  Jira key.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Runs build/test/lint in the local Docker dev environment.
  Reads the story, the approved story-design note, and the failing tests from test-creator.
  Git used for the story branch; Atlassian MCP optional (to read/transition the story).
model: sonnet   # tier: standard (haiku=cheap, sonnet=standard, opus=strong)

---

# implement — Make It Green (TDD green phase)

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


Writes the production code that turns the failing tests green. The tests from `test-creator` are
the spec; this skill satisfies them within the design approved in `story-design`. The discipline
that matters: make the tests pass by **building the feature**, never by weakening the tests.

> The tests are the contract. If a test seems wrong, that's a conversation (fix the story/design
> + the test), not a quiet edit to make red go green.

## Input it expects
- The user story and its approved `story-design.md (in the story folder: docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/)` (interfaces, approach, reuse).
- The failing tests + `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md` from `test-creator`.
- The existing codebase and conventions (from `project-scaffold`).

## Output it produces
- Production code in the target module(s) from the design note, on branch
  `feat/<STORY_KEY>-<slug>`.
- All of the story's tests passing, the full suite still green, lint clean.
- A short implementation summary for the PR (what changed, which tests pass, requirements).

## Procedure

### Step 1 — Create the story branch
Fetch `origin` and branch from `origin/main`: `feat/<STORY_KEY>-<short-slug>` (e.g.
`feat/LEARN-21-upload-course-materials`). All work happens here.

If the developer already has local uncommitted or unpushed work on `main` that should be part of
the story, commit or stash it before creating the story branch; this step intentionally starts
from the remote main line so the branch base is always the latest merged code.
If the workspace is already on a different story branch, do not continue there by accident:
create or switch to the expected `feat/<STORY_KEY>-<short-slug>` branch from `origin/main`
before writing code. Never reuse a previous story's branch for a new story.

### Step 2 — Re-read the design and the tests
Confirm the interfaces in the story-design note match what the tests call. If they've diverged
(e.g. the test expects a different signature), resolve it now — usually the design+test are
right and you build to them; if the design itself was wrong, that's a quick loop back to
`story-design`, not an ad-hoc deviation.

### Step 2.5 — Load the domain lens for this story (backend / frontend / full-stack)
Read the story's **domain tag** from the story-design note (`story-design.md` records it as
backend / frontend / full-stack; if missing, infer from the target modules — frontend if it
targets UI/components, backend if it targets services/API, full-stack if both). Then load the
matching lens(es), which carry the domain craft and point at the project's source-of-truth docs:
- **backend** → `references/backend.md` (contracts, validation, persistence, security NFRs).
- **frontend** → `references/frontend.md` — and READ `docs/ui-conventions.md`, the project's UI
  source of truth (component structure, design tokens, state pattern, accessibility, the
  loading/error/empty/success states). Apply it so UI comes out consistent, not ad hoc.
- **full-stack** → BOTH lenses; build the backend slice and the UI slice, and ensure the UI
  consumes the real API contract from `docs/api-contracts/` (not a mock that drifts).
The lens doesn't change the TDD mechanics below — it raises the quality/consistency bar for the
domain and tells you which project docs to honour.

### Step 3 — Implement the minimum to satisfy the tests
Write code in the module(s) the design specifies. Guidance:
- Build to the approved interfaces; reuse the existing functions/components the design named.
- Do the simplest thing that makes the failing tests pass and matches the acceptance criteria —
  not a speculative framework for future stories (that's the over-engineering trap).
- Handle the unhappy paths the tests assert (exact messages, status codes, no-side-effect on
  rejection). For UI, handle the loading/error/empty states the frontend lens requires.
- Add the data-model migration from the design note if one was specified.
- Follow the project's conventions and lint rules from the scaffold, plus the domain lens
  (ui-conventions.md tokens for UI; contracts/NFRs for backend).
- For stories that create or mutate persisted platform state, write to the declared system of
  record. Do not satisfy a core-entity story with a file, an in-memory collection, or a
  container-local sidecar unless the approved design explicitly says that is the durable store.
  If the codebase lacks the real persistence path, stop and escalate back to `story-design` /
  `tech-design` rather than introducing a shim.

### Step 4 — Run tests until green, then run the whole suite
Iterate: run the story's tests, fix code, repeat until they pass. Then run the **full** suite to
ensure nothing regressed, and run lint. Green here means: this story's tests pass AND the
pre-existing suite still passes AND lint is clean.

### Step 5 — Self-check before handing off
- [ ] Every test from test-creator passes; none were weakened, skipped, or deleted to go green.
- [ ] Full suite green; no regressions.
- [ ] Lint/format clean.
- [ ] Code lives where the design said; no unplanned architecture change crept in.
- [ ] Unhappy paths actually behave as asserted (not just the happy path).

### Step 6 — Hand off
Write the implementation summary. Tell the user the next step is `test-verify` (an independent
gate confirming green + coverage) and then `pr-create`. Leave the branch ready; do not open the
PR here.

## Rules that keep TDD honest
- **Never weaken a test to pass.** No deleting assertions, loosening expected values, skipping,
  or xfail-ing to fake green. If a test is genuinely wrong, fix the story/design and the test
  deliberately, with the change visible.
- **No silent architecture changes.** If making the test pass seems to require a new
  integration/service/cross-cutting concern that isn't in the design, stop and escalate to
  `story-design` / `tech-design` — don't bend it inside the implementation.
- **Minimum viable code.** Resist building for hypothetical future stories; the next story will
  ask for what it needs when it arrives.
- **No fake persistence.** When the story is about a core entity, the implementation must land in
  the same durable store the architecture names. A passing test suite built on a shim is a
  failure of the story, not a success of implementation.

## Reference files
- `templates/implementation-summary.md.tmpl` — the hand-off summary (feeds the PR body).
- `references/green-phase-discipline.md` — making tests pass the right way; anti-patterns.
- `references/backend.md` — backend/API domain lens (loaded for backend/full-stack stories).
- `references/frontend.md` — UI domain lens (loaded for frontend/full-stack; reads ui-conventions.md).
- `examples/` — an implementation summary for story LEARN-21.
