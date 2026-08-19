---
name: project-scaffold
description: >
  Turn an approved technical design into a runnable GREENFIELD repository skeleton: folder
  structure, chosen frameworks wired up, linting/formatting, a working test harness configured
  for TDD, CI config, Dockerfile/compose, and a trivial end-to-end "walking skeleton" that
  builds and passes one test. Use this as the one-time bootstrap step right after `tech-design`
  is approved and before any story is implemented. Trigger this even when the user just says
  "scaffold the project", "set up the repo", "create the project skeleton", "bootstrap the
  codebase", "initialise the repository", or "get the repo ready to build". This skill creates
  the empty-but-running foundation that `implement` writes into; it does NOT implement features.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Runs shell/build/test commands in the local Docker dev
  environment. Reads the committed design from `tech-design`. GitHub used for the initial repo
  and branch conventions; Atlassian MCP optional.
model: sonnet   # tier: standard (haiku=cheap, sonnet=standard, opus=strong)

---

# project-scaffold — Greenfield Repository Skeleton

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


The step that makes the codebase *exist*. After `tech-design` is approved, this skill creates
the repository the rest of the chain builds into: the structure, the wired-up frameworks, and
crucially a **walking skeleton** — the thinnest end-to-end path that actually builds and passes
one test. Until that exists, `implement` has nowhere to put code and TDD has no harness to run.

> Run this ONCE per repo (or once per service if the design is multi-repo). It does not build
> features. Its definition of done is "an empty project that builds, lints, and passes a
> trivial test, with TDD ready to go."

## Input it expects
The committed design from `tech-design`:
- `tech-stack.md` (languages, frameworks, pinned versions)
- `architecture.md` (the module/service layout to mirror in folders)
- `testing-strategy.md` (the TDD setup and runners to configure)
- `data-model.md` / `api-contracts/` (so initial folders and types line up — not implemented,
  just placed)

If any are missing, stop and point the user back to `tech-design` rather than guessing the
stack.

## Output it produces
A runnable repository containing at least:
1. **Folder structure** mirroring `architecture.md` (modules/services, web, contracts, infra,
   docs).
2. **Frameworks wired up** — backend app boots, frontend app builds, per the approved stack.
3. **Linting & formatting** configured with sensible defaults and a one-command check.
4. **Test harness for TDD** — the runners from `testing-strategy.md` installed and runnable,
   with one trivial passing test per layer so the harness is proven.
5. **Walking skeleton** — one trivial end-to-end path (e.g. a `/health` endpoint the frontend
   can call, or a single rendered page) with one test that exercises it.
6. **CI config** — runs lint + tests on every PR (local-runner friendly).
7. **Containerisation** — Dockerfile(s) + `docker-compose.yml` that brings the stack up
   locally per the deployment decision.
8. **Repo hygiene** — README (how to run, test, contribute), `.gitignore`, `.env.example`,
   branch-naming + PR-template conventions, ADRs copied into `docs/adr/`.
9. **`AGENTS.md` at the repo root** — the living orientation file, including the **Project Layout**
   table that is the single source of truth for where every artifact lives. The whole skill chain
   resolves paths from this table, so creating it here (with the `docs/` tree above) is what makes
   every downstream skill find its inputs. `design-sync` keeps it current thereafter.

## Procedure

### Step 1 — Read the approved design
Load `tech-stack.md`, `architecture.md`, `testing-strategy.md`. Derive the exact folder layout
from the architecture's components. Confirm the pinned versions; if a version looks stale,
flag it (don't silently upgrade).

### Step 2 — Create structure & wire frameworks
Lay down the folders mirroring the architecture. Initialise each app with its framework so it
boots/builds. Keep modules empty but present (a stub per architecture component) so `implement`
has obvious homes for code.

Also create the **`docs/` artifact tree** that the whole skill chain reads/writes, matching the
Project Layout table in `AGENTS.md`:
```
docs/
├── architecture.md  data-model.md  tech-stack.md  testing-strategy.md  ui-conventions.md
├── CHANGELOG-design.md       product-overview.md
├── adr/                      # ADR-NNN-*.md (copied from tech-design)
├── api-contracts/            # *.openapi.yaml (the interface specs — the source of truth)
├── design-history/           # tech-design-proposal.md
└── product/                  # PRD.md, epics.md, epic-map.json
    └── epics/<EPIC_KEY>/      #   _epic/ (stories.md, story-map.json, epic-sequence.*)
        └── stories/<STORY_KEY>/   #   story-design.md, test-plan.md, verification.md, …
```
API contracts live under `docs/api-contracts/` as the spec source of truth. If the stack also
needs generated/shared types as a code package, generate them FROM those specs into the app's
own package at build time — don't hand-maintain a second copy. Keep `AGENTS.md` at the repo root
(not under `docs/`), since agents auto-discover it there.

If the PRD, epics, and stories were produced before this repo existed (the usual order: `prd` →
`prd-decompose` → `user-story` run before scaffolding), move those existing files into
`docs/product/` now so they sit at their canonical location, and record that location in the
Project Layout table. From here on, every skill reads/writes them there.

### Step 3 — Configure linting, formatting, and the TEST HARNESS
Install and configure the linter/formatter with a one-command check. Then set up the test
runners named in `testing-strategy.md`:
- Add one trivial passing test per layer (unit, integration, e2e) to prove the harness runs.
- Wire test commands into the project's task runner / scripts.
Because the project is TDD, the harness existing and being trivially green is a hard
requirement — it's what every later story's failing test will run against.

**Test layout — mirror the source tree (scales to hundreds of stories).** Establish this
convention now so tests don't pile up flat:
```
tests/
├── unit/<module>/test_*.py          # mirrors app modules: app/<module>/x.py -> tests/unit/<module>/test_x.py
├── integration/<module>/test_*.py   # same module sub-foldering
├── e2e/test_*.py                    # user-journey level; module sub-folders optional
└── conftest.py                      # shared fixtures (per-folder conftest.py allowed for local fixtures)
```
Rules:
- **The test tree mirrors the source tree.** A test for `app/<module>/<file>.py` lives at
  `tests/unit/<module>/test_<file>.py` (and integration equivalently). This bidirectional mapping
  is the whole point — a developer finds a test wherever the code is.
- **Test-type stays at the top** (unit / integration / e2e) so the pyramid is visible and you can
  run a layer in isolation (fast unit in pre-commit, slow e2e in CI).
- Create a `<module>/` sub-folder under unit/ and integration/ for **each architecture module**
  from `architecture.md`, even if empty initially (with a `conftest.py` or `__init__.py` as the
  runner needs), so `test-creator` has an obvious home per module.
- Configure test discovery to recurse sub-folders (pytest does by default; ensure no flat-only
  config). Document the convention in `testing-strategy.md` and `AGENTS.md`.
Put the walking-skeleton tests in the right places too (Step 4): the trivial unit test under
`tests/unit/<core-module>/`, the integration health test under `tests/integration/<core-module>/`,
the e2e under `tests/e2e/`.

### Step 4 — Build the walking skeleton
Implement the single thinnest end-to-end slice: e.g. backend `/health` → returns ok; frontend
calls it and renders the result; one e2e test asserts the round trip. This proves the wiring
(build, run, container, test) actually works before any feature is added. Nothing more.

### Step 5 — Containerise and verify it runs
Write Dockerfile(s) and `docker-compose.yml` per the deployment decision. Then actually run it:
`docker-compose up`, hit the walking-skeleton path, run the test suite. The skill is not done
until the stack comes up and the trivial tests pass. Capture the commands in the README.

### Step 6 — CI, hygiene, AGENTS.md, and first commit
Add CI that runs lint + tests on PRs. Write the README (run/test/contribute), `.gitignore`,
`.env.example`, a PR template, and the branch-naming convention (e.g. `feat/LEARN-21-...`).
Copy the ADRs from `tech-design` into `docs/adr/`. Create **`AGENTS.md` at the repo root** from
the design-sync `AGENTS.md` template, filling in the stack and module map and — critically — the
**Project Layout** table pointing at the `docs/` tree created in Step 2. This table is what every
downstream skill uses to resolve artifact paths, so it must be present and correct before the loop
starts. Make the initial commit / open the repo.

### Step 7 — Hand off
Report what was created and the verification result (build + tests green, stack runs). Tell the
user the repo is ready for the per-story loop: for each story, `test-creator` writes failing
tests from its acceptance criteria, then `implement` makes them pass.

## Definition of done (verify, don't assume)
- [ ] Project builds with one command.
- [ ] Linter runs clean with one command.
- [ ] Test suite runs and the trivial tests pass (TDD harness proven).
- [ ] `docker-compose up` brings the stack up and the walking-skeleton path responds.
- [ ] CI runs lint + tests on PRs.
- [ ] README documents run/test/contribute; ADRs present in docs/.
- [ ] No feature logic implemented — only the skeleton.

## Reference files
- `templates/README.md.tmpl` — project README skeleton.
- `templates/PULL_REQUEST_TEMPLATE.md` — the PR template to install.
- `references/scaffold-checklist.md` — the verification checklist (run it before handing off).
- `references/walking-skeleton.md` — what the thinnest end-to-end slice should and shouldn't be.
- `examples/` — a scaffold plan + verification log for the Learn Portal MVP stack.
