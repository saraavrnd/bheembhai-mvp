---
name: test-verify
description: >
  Independently verify that a story's implementation is genuinely done before a PR is opened:
  confirm the full test suite passes, that every acceptance scenario is actually covered by a
  test, that no test was weakened/skipped to fake green, and that lint and coverage gates hold.
  Use this as the fourth step of the per-story delivery loop, after implement and before
  pr-create. Trigger this even when the user just says "verify the tests", "check it's really
  green", "is LEARN-21 done", "run the gate", or "confirm before PR". This skill is a read-only
  gate — it runs and inspects, it does NOT write features or tests, and it can BLOCK the loop.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Runs the project's test/lint/coverage commands against
  the deployed Docker Compose stack for integration and browser coverage, while unit tests
  remain in-process. Reads the story, its test-plan, and the implementation on the branch. Does
  not modify code.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# test-verify — The Honest-Green Gate

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


A separate pair of eyes between `implement` and `pr-create`. `implement` is motivated to reach
green; this skill's only job is to confirm the green is *real* — that the feature is actually
built and tested, not that the tests were bent to pass. It can and should **block** the loop
when something is off. Keeping verification independent of implementation is what makes the gate
meaningful.

> This skill does not fix anything. It verifies and reports a clear pass/block with reasons. If
> it blocks, the fix goes back to `implement` (or `test-creator`), then re-verify.

## Input it expects
- The story + its `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md` (the scenario → test mapping from test-creator).
- The implementation on branch `feat/<STORY_KEY>-...` and the implementation summary.
- The project's test/lint/coverage commands (from `testing-strategy.md`).

## Output it produces
- A `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md` report: PASS or BLOCK, with evidence.
- On BLOCK: a precise list of what failed and where to fix it (implement vs test-creator).
- On PASS: confirmation that the story is ready for `pr-create`.

## Procedure (all read-only)

### Step 1 — Run the full suite, lint, and coverage
Run the entire test suite (not just this story's tests — catch regressions), the linter, and the
coverage report. Capture raw output and preserve the executed test names in the report. Use the
most verbose practical mode for the test runner (`pytest -vv` instead of `-q`) so reviewers can
see exactly which tests ran and passed. For integration and browser tests, ensure the deployed
Docker Compose stack is up first and target that running instance. A pass requires: full suite
green, lint clean, coverage gate (if defined) met.

### Step 2 — Confirm acceptance-scenario coverage
Cross-check `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/test-plan.md` against the actual tests in the branch:
- Every Gherkin scenario maps to at least one test that EXISTS and RUNS.
- Unhappy-path scenarios are present, not just happy paths.
A story whose suite is green but whose tests don't cover all scenarios is NOT done — block.

### Step 3 — Detect faked green (the key check)
Inspect for the anti-patterns that make a suite pass dishonestly:
- Tests skipped / xfail / disabled / commented out since test-creator wrote them.
- Assertions deleted or loosened versus the acceptance criteria (e.g. exact error message
  downgraded to a substring or removed).
- Tests deleted relative to the test-plan.
- Production code that special-cases the test's input rather than implementing the behaviour.
- Errors swallowed so an unhappy-path test stops failing without the behaviour existing.
If any are found, BLOCK with the specific file/line and route the fix back.

### Step 4 — Confirm no regression and design adherence
- The pre-existing suite still passes (diff the failing/passing set against main if possible).
- Code landed in the module(s) named in the story-design note; no unplanned new
  service/integration crept in. If the architecture appears to have changed, block and route to
  `story-design`/`tech-design`.

### Step 5 — Verdict
Write `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/verification.md`:
- **PASS** — full suite green, all scenarios covered, no faked green, no regression, design
  adhered to. Ready for `pr-create`.
- **BLOCK** — list each issue, its evidence, and the owning step to fix it. The loop stops here
  until re-verified.

The verification report should preserve the current summary structure, and it should also include
the full raw command evidence for each verification command run. Prefer separate evidence blocks
for the test suite, lint, and coverage commands so reviewers can see the executed test names and
the exact command output without losing the summary table. Do not use quiet mode for the test
runner when generating the verification report.

### Step 6 — Hand off
On PASS, tell the user the story is verified and the next step is `pr-create`. On BLOCK, state
exactly what must change and that re-verification is required after the fix.

## Why this is separate from implement
If the same step both writes code and declares it done, "done" drifts toward "tests are green by
whatever means." An independent gate that inspects for weakened tests and missing coverage keeps
"done" honest. The cost is one extra run; the benefit is that a PASS actually means something.

## Reference files
- `templates/verification-report.md.tmpl` — the PASS/BLOCK report.
- `references/faked-green-checklist.md` — the anti-patterns to inspect for.
- `examples/` — a PASS verification for story LEARN-21.
