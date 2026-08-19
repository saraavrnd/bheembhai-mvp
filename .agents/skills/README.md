# PDLC Skill Chain (product-agnostic)

A pipeline of reusable skills that take **any** product idea through the product development
lifecycle using Claude or Codex. Each skill consumes the previous skill's output artifact and
produces the next, so the chain composes into a semi-autonomous build flow. These skills are
generic — the product (Learn Portal, a payments API, anything) is just the input you feed in.

## The chain

```
  idea / domain notes
        │
        ▼
   ┌─────────┐   PRD.md
   │   prd   │ ───────────────►
   └─────────┘
        │
        ▼
 ┌───────────────┐  epics in Jira + epic-map.json
 │ prd-decompose │ ──────────────────────────────►   (via Atlassian MCP)
 └───────────────┘
        │
        ▼
 ┌────────────┐  stories in Jira + story-map.json
 │ user-story │ ─────────────────────────────────►   (via Atlassian MCP)
 └────────────┘
        │
        ▼
 ── greenfield bootstrap (run once) ──────────────────────────
 ┌─────────────┐  approved design: architecture, ADRs, contracts, testing strategy
 │ tech-design │ ───────────────────────────────────────────►   (propose → APPROVE)
 └─────────────┘
        │
        ▼
 ┌─────────────────┐  runnable repo skeleton + walking skeleton + TDD harness
 │ project-scaffold│ ─────────────────────────────────────────►
 └─────────────────┘
        │
        ▼
 ── per-story loop (repeat for each story, TDD) ───────────────
 ┌──────────────┐  story-design-<KEY>.md  (the "how", REVIEWED)
 │ story-design │ ──────────────────────────────────────────►   (propose → APPROVE)
 └──────────────┘
        │
        ▼
 ┌──────────────┐  failing tests + test-plan  (TDD red)
 │ test-creator │ ──────────────────────────────────────────►
 └──────────────┘
        │
        ▼
 ┌───────────┐  code on feat/<KEY> branch, tests green  (TDD green)
 │ implement │ ─────────────────────────────────────────────►
 └───────────┘
        │
        ▼
 ┌─────────────┐  verification report: PASS / BLOCK  (honest-green gate)
 │ test-verify │ ───────────────────────────────────────────►
 └─────────────┘
        │ PASS
        ▼
 ┌─────────────┐  advisory review report + fix list  (standards/security/acceptance/maintainability)
 │ code-review │ ───────────────────────────────────────────►
 └─────────────┘
        │ fixes? ──► implement applies ──► test-verify re-runs ──┐
        │                                                        │
        │ (none / waived)                                        │
        ▼                                                        │
 ┌───────────┐  design-sync commits doc/AGENTS.md updates to     │
 │ pr-create │  the branch → GitHub PR (via MCP) + Jira → In     │
 │ +design-  │  Review (via MCP); flags affected open stories    │
 │  sync     │ ◄───────────────────────────────────────────────┘
 └───────────┘
        │
        └──► human review owns merge ──► next story starts at story-design

   ↑ escalation: if a story needs an architecture change, story-design kicks back to
     tech-design (AMEND mode) → new ADR → resume. (before code, not at merge)
```

### Built now
| Skill | In | Out |
|-------|----|----|
| `prd` | idea / domain notes / whitepaper | `PRD.md` — numbered, atomic, testable requirements grouped into feature areas |
| `prd-decompose` | `PRD.md` | Jira **epics** (via Atlassian MCP) + `epic-map.json` traceability |
| `user-story` | an epic + `epic-map.json` | Jira **stories** with Gherkin acceptance criteria + `story-map.json` |
| `tech-design` | PRD + epics (create); escalated story (amend) | **approved** architecture, ADRs, data model, API contracts, tech stack, testing strategy. **Amend mode**: updates the design + adds an ADR when a story needs an architecture change |
| `project-scaffold` | approved design | **runnable repo skeleton** with frameworks wired, TDD harness, walking skeleton, CI, Docker |
| `story-design` | a story + committed design | thin, **reviewed** `story-design-<KEY>.md`; escalates to tech-design (amend) if the story breaks the architecture |
| `test-creator` | story + approved story-design | **failing tests** (TDD red) + `test-plan-<KEY>.md`, one+ test per acceptance scenario |
| `implement` | failing tests + story-design | code on `feat/<KEY>` branch making tests **green**, no test weakened |
| `test-verify` | branch + test-plan | **PASS/BLOCK** verification: honest-green gate (coverage, no faked green, no regression) |
| `code-review` | verified branch + standards/NFRs | **advisory** report (standards + security + acceptance + maintainability) by severity + fix list routed to `implement`; blocks nothing |
| `design-sync` | merged story diff + committed design + AGENTS.md | updates **architecture / data-model / contracts / AGENTS.md** on the branch (proportionate; no-op when nothing changed) + **flags affected in-progress stories** via Jira |
| `pr-create` | verified (and reviewed) story | runs `design-sync`, then GitHub **PR** (via MCP) + Jira story → **In Review** (via MCP); does not merge |

### Utility skills
| Skill | In | Out |
|-------|----|----|
| `epic-review` | an epic's full story set (Jira key or `stories.md`) + committed design | cross-story gaps drilled via interview; every affected story rewritten together in place + `epic-review.md` audit log; escalates architecture-conflict gaps to `tech-design` |
| `story-review` | a written story (Jira key or pasted) + committed design | gaps drilled via interview; story's acceptance criteria rewritten in place + `story-review.md` audit log; escalates architecture-conflict gaps to `tech-design` |
| `revert-run` | `run_id` / story key / branch + local run ledger | human-approved preview + git rollback / revert record in the local audit trail |

### The two phases
**Bootstrap (run once):** `tech-design` → `project-scaffold`. Establishes the stack/architecture
(with your approval) and a repo that builds, lints, tests, and runs in Docker.

**Per-story loop (repeat forever, TDD):** `story-design` → `test-creator` → `implement` →
`test-verify` → `code-review` → `pr-create` (which runs `design-sync`). Two human checkpoints per
story: the story-design review and the PR review.

### Entry point: `story-implement` (meta-skill)
You don't invoke the six loop skills by hand. **`story-implement`** is the single entry point —
invoke it with a story key ("implement LEARN-21") and it orchestrates the six in order, pauses at
the two human checkpoints (story-design approval, PR review), and handles the internal loops
(TDD red/green inside `implement`, the `test-verify` BLOCK loop, the `code-review` fix loop, and
architecture escalation to `tech-design`). It delegates to the six skills — it doesn't re-implement
them — and stops at the open PR (it never merges). Initiate each story by invoking it again with
the next key; run it separately per story for parallel work.

### Ordering stories: `epic-sequence`
Before implementing an epic's stories, run **`epic-sequence`** to order them by dependency so a
producer (the story that creates an endpoint/table) is built before its consumers. It detects
dependencies from three sources (explicit Jira links > story-design artifacts > requirement/data
model), topologically sorts into **waves** (same-wave stories are independent → parallelizable),
flags cycles, proposes the order for approval, then sets Jira "blocks" links and writes a
machine-readable plan. `story-implement` reads that plan: it refuses to start a story whose
prerequisites aren't merged, and can auto-pick the next eligible story. Flow:
`user-story → epic-review → epic-sequence → story-implement (wave by wave)` — running
`epic-review` first means `epic-sequence` sorts a story set whose cross-story contradictions and
missing dependency notes are already resolved, rather than baking them into the dependency graph.

### Keeping the design current (anti-drift, for parallel teams)
Two distinct paths keep the system design and `AGENTS.md` from going stale as multiple developers
work in parallel:
- **Reactive (before code):** a story that needs an architecture change is caught by
  `story-design`'s escalation rule and routed to `tech-design` **amend mode**, which updates the
  design and records a new ADR before implementation proceeds.
- **Housekeeping (at every merge):** `design-sync` runs inside `pr-create` on every story,
  reconciling the references and `AGENTS.md` with what shipped and committing those updates onto
  the same branch so they merge atomically. It's proportionate (a near no-op for most stories)
  and **flags any in-progress stories the change affects**, so parallel work re-aligns proactively
  instead of colliding at merge. `AGENTS.md` is treated as a first-class output because every
  future story's agent reads it first.

### Sharpening a whole epic before its stories scatter: `epic-review`
On-demand, and cheapest run right after `user-story` finishes an epic — before `epic-sequence`,
before any per-story `story-review`, before `story-design` touches anything. `story-review` drills
one story in isolation; some gaps only exist *across* siblings — the same actor/role several
stories assume but that's never actually defined in the data model, a requirement two stories
claim in contradicting ways (or that no story claims at all), a cross-epic dependency a story
quietly assumes but never writes down, a one-sided "depends on me" claim no sibling reciprocates.
`epic-review` loads the epic's **full** story set together, drills
`references/gap-checklist.md`'s cross-story categories, and rewrites every affected story in the
same pass — so the same question doesn't get asked (and answered differently) once per story.
Logs the interview to `epic-review.md` in the epic's `_epic/` folder. Same escalation posture as
`story-review`: a gap that's a real architecture conflict (not a legitimate, already-sequenced
cross-epic dependency) gets flagged to `tech-design` rather than resolved here. It does not
compute build order — that's still `epic-sequence`'s job.

### Sharpening a story before design: `story-review`
On-demand, not an automatic stage. Whenever a story reads as ambiguous — right after `user-story`
produces it, before `story-design` starts, or on an old backlog story nobody's touched — run
`story-review`. It reads `architecture.md`, `data-model.md`, `api-contracts/`, and sibling stories
in the same epic, drills the story with pointed questions grounded in what actually exists (not
generic "any ambiguity?" prompts), then rewrites the acceptance criteria in place with the user's
sign-off and logs the interview to `story-review.md` in the story's folder. If a gap turns out to
be an architecture conflict rather than story ambiguity, it flags that for `tech-design` (amend
mode) instead of resolving it — the same escalation posture `story-design` uses, just one step
earlier and cheaper.

### Possible later additions
Deploy/release skills once stories are merging (e.g. a `release` skill that cuts a versioned
build and brings the Docker stack up), and a periodic dependency/security re-scan skill.

## Cost & model tiers
Each skill declares a `model:` tier in its frontmatter — `cheap`, `standard`, or `strong` —
routing it to the cheapest model that does its job well. High-judgment skills (`tech-design`,
`story-design`, `code-review`, `prd`) are `strong`; mechanical ones (`epic-sequence`,
`test-creator`, `test-verify`, `design-sync`, `pr-create`) and the `story-implement` orchestrator
are `cheap`; coding/verification sit at `standard`. The tiers are generic; map them to your actual
models once in `_SHARED-cost-and-models.md` (kept out of the skills so model renames don't churn
them). Three cost levers are baked in:
1. **Per-skill model tier** — don't pay strong-model rates for traffic-directing.
2. **Read slices, not whole files** — skills load only the sections they act on (this fixes the
   `epic-sequence` context blowup that forced 1M-context fallback).
3. **Keep MCP results small** — request minimal Jira/GitHub fields, batch reads, don't re-fetch;
   `/compact` between stories to flush accumulated tool results. (MCP results persist for the whole
   session, which is why the Atlassian server dominated usage.)

## Where artifacts live (single source of truth)
Project artifacts live under `docs/` in the product repo; their canonical paths are declared in
the **Project Layout** table inside `AGENTS.md` at the repo root. Every skill resolves artifact
locations from that table (see `_SHARED-project-layout.md`) instead of hardcoding paths — so when
the layout moves, you change one table, not twelve skills. Current layout:

```
AGENTS.md                      # repo root — orientation + Project Layout table (source of truth)
docs/
├── architecture.md  data-model.md  tech-stack.md  testing-strategy.md
├── CHANGELOG-design.md  product-overview.md
├── adr/                       # ADR-NNN-*.md
├── api-contracts/             # *.openapi.yaml  (interface specs — source of truth)
├── design-history/            # tech-design-proposal.md
└── product/
    ├── PRD.md  epics.md  epic-map.json        # project-level
    └── epics/<EPIC_KEY>/
        ├── _epic/             # stories.md, story-map.json, epic-sequence.{md,json}, epic-review.md
        └── stories/<STORY_KEY>/   # story-review.md, story-design.md, test-plan.md, verification.md,
                                    #   code-review.md, design-sync.md  (short names, folder carries key)
```

**Per-story folders (scales to hundreds of stories):** every artifact for one story lives in its
own folder `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` with short, un-suffixed filenames.
A story's files are resolved under its folder only; epic-level files sit in the epic's `_epic/`
folder; project-level files (PRD, epics, epic-map) stay at `docs/product/`. Use `epic-map.json` to
find a story's epic from its key.

`project-scaffold` creates this tree and the `AGENTS.md` table; `design-sync` keeps the table
honest at each merge. **Skill-internal files** (`templates/`, `references/`, `examples/`,
`scripts/` inside each skill) are tooling — they stay in the skill folder wherever the agent
loads skills from and are never placed under `docs/`.

## Why it holds together: the traceability thread
Requirement IDs flow through every step:

```
PRD requirement (FR-006)
  → epic-map.json   : LEARN-11 covers FR-006
    → story-map.json: LEARN-23 covers FR-006
      → (later) tests and code reference LEARN-23 / FR-006
```

So at any point you can answer "is requirement X built and tested?" by following the IDs.
`prd-decompose` enforces that every requirement maps to exactly one epic; `user-story`
enforces that every epic requirement is covered by at least one story.

## Jira via Atlassian MCP
The Jira-touching skills (`prd-decompose`, `user-story`) create issues through the **connected
Atlassian MCP**, not a REST script. They discover the project and issue types, create issues
one at a time, capture returned keys, and fall back to a CSV import file if the MCP isn't
connected. See `references/atlassian-mcp-usage.md` in those skills.

## How to run it
Install where your agent reads skills (e.g. `.claude/skills/`), then drive the chain in order:

1. "Here are my product notes — **write a PRD**."  → `prd` produces `PRD.md`.
2. "**Decompose the PRD into epics** in project LEARN."  → `prd-decompose` creates epics.
3. "**Break LEARN-11 into stories**."  → `user-story` creates stories with acceptance criteria.

## Worked example (Learn Portal)
Each skill ships a real example produced from the Learn Portal whitepaper:
- `prd/examples/PRD.md` — 8 feature areas, 18 FRs, 5 NFRs.
- `prd-decompose/examples/epics.md` + `epic-map.json` — 8 epics, verified 1:1 coverage of all 23 requirements.
- `user-story/examples/stories-LEARN-11.md` + `story-map-LEARN-11.json` — epic LEARN-11 expanded into 3 stories with Gherkin criteria.

## Conventions
Tool-agnostic (Claude Code + Codex), Jira via Atlassian MCP, artifact-driven hand-offs,
ID-based traceability end to end. Skills are product-agnostic and reusable across projects.

**Test layout** mirrors the source tree so tests stay maintainable at scale: test-type at the top
(`tests/unit|integration|e2e/`), module sub-folders under each (`tests/unit/<module>/test_*.py`),
mapping `app/<module>/x.py` → `tests/unit/<module>/test_x.py`. `project-scaffold` establishes it;
`test-creator` places each story's tests under the right module folder (never flat). Tests track
code structure, not ticket structure — the per-story `test-plan.md` links tests back to the story.

**Backend vs UI stories** are handled by domain tagging, not separate skills. `story-design` tags
each story **backend / frontend / full-stack**. `implement` then loads the matching domain lens —
`references/backend.md` (contracts, validation, persistence, security) and/or
`references/frontend.md` (which reads `docs/ui-conventions.md` for component structure, design
tokens, state pattern, required loading/error/empty/success states, and accessibility). The
project's UI knowledge lives in `docs/ui-conventions.md` — a thin starter `tech-design` proposes
(since most projects begin with no design system) that `design-sync` grows by promoting patterns UI
stories establish. `test-creator` writes API/integration tests for backend, component/e2e for UI.
One `implement` skill, domain-aware via lenses + a project UI-context artifact.
