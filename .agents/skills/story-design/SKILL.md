---
name: story-design
description: >
  Produce a thin, reviewable per-story design note before any tests or code are written: decide
  which existing architecture module(s) a user story touches, the interface/endpoint signatures,
  the data-model deltas, and what existing code to reuse — staying consistent with the committed
  tech-design rather than reinventing it. Use this as the FIRST step of the per-story delivery
  loop, before test-creator. Trigger this even when the user just says "design this story",
  "how should we build LEARN-21", "plan the implementation for this story", "what's the approach
  for this ticket", or names a story to start. This skill PROPOSES a short design and pauses for
  approval; it does not write tests (test-creator) or code (implement). It also detects when a
  story exceeds the existing architecture and escalates back to tech-design instead of bending it.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Reads architecture.md / api-contracts / data-model.md
  from the committed tech-design and the existing codebase. Atlassian MCP optional (to read the
  story). Output is a short Markdown design note that test-creator and implement consume.
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# story-design — Thin Per-Story Design (propose → review)

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


The "how" for one story. `tech-design` decided the system-level design once; this decides how a
single story fits into it. It is deliberately **thin** — proportionate to the story — and it is a
**review checkpoint**: correcting a half-page note costs minutes, correcting code+tests costs
hours, so this is the cheapest place to catch a misalignment. It pauses for approval before the
loop proceeds.

> This step exists so the interface/endpoint/data decisions are explicit and reviewable BEFORE
> test-creator writes tests against them. Tests written against a guessed signature get thrown
> away when implement picks a different one; this prevents that waste.

## Input it expects
- A user story with its Gherkin acceptance criteria (from `user-story`) — by Jira key (read via
  Atlassian MCP) or pasted.
- The committed design from `tech-design`: `architecture.md`, `api-contracts/`, `data-model.md`.
- The existing codebase (after `project-scaffold`), so reuse is real, not assumed.

## Output it produces
Two things:
1. One short file: `story-design.md` written to **this story's folder**,
   `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/story-design.md` (create the folder if it
   doesn't exist; resolve `<EPIC_KEY>` via `docs/product/epic-map.json` if you only have the story
   key). From `templates/story-design.md.tmpl`,
   **presented for approval** before the loop continues. It is intentionally small.
2. A **dependency-delta** for `epic-sequence`: the firm produces/consumes this design revealed, so
   the (provisional) epic order can self-correct. This is what resolves the sequencing paradox —
   the precise dependency signal flows from design back to sequence. See Step 6.

## Procedure

### Step 1 — Read the story and the system design
Get the story's acceptance criteria. Read `architecture.md` to find which module(s) own this
behaviour, `api-contracts/` for existing interface shapes, and `data-model.md` for the entities
involved. Skim the actual code in the target module(s) so reuse is grounded.
For any story that creates, updates, or authenticates a persisted core entity
(`User`, `Project`, `Run`, `Approval`, `Membership`, configuration snapshots, or other
source-of-truth state), also verify the concrete persistence path exists in code. Name the actual
model/repository/table/migration that owns the data. If the codebase only has a shim
(file-backed, in-memory, or container-local state) or the real persistence path is missing,
call that out explicitly and escalate back to `tech-design` rather than papering over it here.

### Step 2 — Decide the "how" (keep it proportionate)
Write the design note covering only what this story needs:
- **Domain tag** — classify the story as **backend** / **frontend** / **full-stack**, from the
  acceptance criteria and target modules: backend if it's pure API/service/data work, frontend if
  it's UI/components, full-stack if it spans both. This tag drives which lens `implement` loads and
  which test types `test-creator` writes (API vs component/e2e). When unsure, infer from the
  target module(s); a story touching both service and UI modules is full-stack.
- **Target module(s)** — where the code lives, per the architecture (no new top-level structure
  unless escalating — see Step 4). For frontend, name the components; for full-stack, both sides.
- **Interfaces / endpoints** — concrete signatures: route + method + request/response shape, or
  function/class signatures, or (UI) components with props/state and the API contract consumed.
  These are what test-creator will test against.
- **Data-model deltas** — new fields/tables/migrations, or "none". Tie to `data-model.md`.
- **Reuse** — existing functions/components/services this builds on, named explicitly.
- **Approach** — 2-5 sentences on the implementation strategy. Not pseudocode for the whole thing.
- **Test surface** — which layers this story needs tests at (unit / integration / e2e / component),
  so test-creator knows the scope. UI stories include component + e2e; backend include unit +
  integration.
- **Out of scope** — restated from the story, to keep the design bounded.

Size guide: a CRUD-ish story → a few lines per section. A meaty story → up to ~half a page.
If the note is growing past a page, the story is probably too big — flag it for splitting back
in `user-story`.

### Step 3 — Check consistency with the committed design
Before presenting, verify: the interfaces match the conventions in `api-contracts/`; the data
deltas are consistent with `data-model.md`; nothing contradicts an existing ADR. If it all fits,
proceed to review.
For stories that touch persistent state, also verify the note names the durable system of record
and that the approach writes there directly. If the design would be satisfied by a temporary shim,
or if the real model/table/migration does not exist yet, stop and escalate. Do not approve a
story-design that says "none" for data-model deltas when the acceptance criteria require a real
database-backed entity change.

### Step 4 — Escalation rule (the important guardrail)
If the story **cannot** be satisfied within the existing architecture — it needs a new external
integration, a new cross-cutting concern, a new service, or it contradicts an ADR — **do NOT
quietly bend the architecture inside this note.** Instead:
- State the gap explicitly in the note under "Architecture impact".
- Recommend kicking back to `tech-design` to update the architecture and record an ADR.
- Pause for the user to decide. Most stories won't trigger this; the ones that do are exactly
  the ones you don't want an agent silently improvising around.

### Step 5 — Present for approval (the checkpoint)
By this step the note must ALREADY be written to `story-design.md` — Steps 1-4 do the actual work
(read inputs, decide the how, write the note). Do not stop before the note exists and ask whether
to start; produce it first. Now SHOW the written note and ask the user to approve or adjust. Do not
hand off to `test-creator` until the design is approved. If the user changes the interface or
approach, update the note and re-confirm — this realignment is the entire value of the step.

> Anti-stall note: don't narrate ("this skill will read the story and produce a note… waiting for
> it to complete") and then pause — that leaves the agent waiting for itself. Read the files and
> write the note in the same turn, THEN pause with the note in hand. The only pause is after the
> artifact exists.

### Step 6 — Hand off (and feed the sequence)
On approval, two things:
- Tell the user the next step is `test-creator`, which will write failing tests against the
  interfaces and acceptance criteria in this note (TDD), followed by `implement`.
- **Emit the dependency-delta to `epic-sequence`.** From this note's produces/consumes, state the
  **firm** dependencies discovered: "this story consumes <endpoint/table/field/module> that
  <other story> produces." If the design reveals a dependency the provisional epic order missed —
  or contradicts one it assumed — call `epic-sequence` in refine mode for the epic so it updates
  the order of the **remaining** (not-yet-merged) stories and adjusts the Jira "blocks" links. If
  the design only confirms the existing order, the delta is a no-op. This is the feedback that
  turns the provisional first sequence into an accurate one as designs land.

  Capture the delta in the note's "Dependencies discovered" section so it's auditable: which
  story-produced things this story consumes, and whether that matched or changed the sequence.

## What this skill is NOT
- Not system architecture — that's `tech-design` (and this escalates to it when needed).
- Not tests — that's `test-creator`.
- Not code or pseudocode dumps — keep it to interfaces + approach.
- Not a license to infer a persistence layer that is absent. If the story depends on a core
  entity and the codebase does not yet have that durable model, surface the gap and route it to
  `tech-design`.

## Reference files
- `templates/story-design.md.tmpl` — the thin note structure.
- `references/design-depth-guide.md` — how much design is enough, and the escalation rule.
- `examples/` — a worked, approved design note for story LEARN-21.
