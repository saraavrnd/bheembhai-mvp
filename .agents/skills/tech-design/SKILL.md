---
name: tech-design
description: >
  Produce the technical design for a GREENFIELD product before any code exists: propose a tech
  stack, architecture, service boundaries, data model, and API contracts derived from the PRD
  and epics, and present them for human approval BEFORE anything is committed. Use this as the
  one-time bootstrap step that sits between user stories and implementation. Trigger this even
  when the user just says "design the architecture", "propose a tech stack", "what should we
  build this in", "create the technical design", "set up the architecture", or asks how to
  start building a new product. This skill also runs in AMEND mode when `story-design` escalates
  a structural change mid-build (a story that needs something the architecture didn't anticipate):
  trigger phrases like "amend the architecture", "update the tech design for this story",
  "the story needs an architecture change", or "add an ADR for this". This skill PROPOSES and
  waits for approval; it does not scaffold code (that is `project-scaffold`) and does not decide
  the stack unilaterally.
compatibility: >
  Tool-agnostic (Claude Code or Codex). May use web search to check current versions/options if
  available, but works without it. Output is Markdown design docs + ADRs + contract files that
  `project-scaffold` and `implement` consume. Atlassian MCP optional (to read epics/stories).
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# tech-design — Greenfield Technical Design (propose → approve)

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


The bridge between "what to build" (PRD/epics/stories) and "how to build it" (code). On a
greenfield project none of the technical foundations exist yet, so this skill establishes
them. Its defining behaviour: **it proposes options with trade-offs and stops for human
approval before anything is locked in.** Approval is a gate, not a formality.

> This skill chooses nothing unilaterally. It recommends, justifies, and presents alternatives,
> then waits. Only after the user approves does it write the final design artifacts that
> `project-scaffold` will build from.

## Input it expects
- `PRD.md` (from the `prd` skill) — requirements, NFRs, constraints.
- The epics (and any stories) — to understand the shape and size of the system.
- Any hard constraints the user states (existing infra, team skills, deployment target,
  budget, compliance). For greenfield these are often light — surface assumptions explicitly.

## Output it produces
Produced in two phases (proposal, then — after approval — the committed design). Write each to
the location given in the Project Layout table in `AGENTS.md`; the current layout is shown below.

**Phase A — proposal (for review):**
1. `docs/design-history/tech-design-proposal.md` — stack options with trade-offs, a recommended
   choice, the proposed architecture, and open decisions needing input.

**Phase B — committed design (after approval):**
2. `docs/architecture.md` — the agreed architecture: components, responsibilities, data flow,
   and diagrams.
3. `docs/adr/ADR-001…NNN.md` — one Architecture Decision Record per significant locked decision.
4. `docs/data-model.md` — entities, relationships, key fields.
5. `docs/api-contracts/` — the interface contracts (OpenAPI yaml) per service.
6. `docs/tech-stack.md` — the final stack with versions, pinned and justified.
7. `docs/testing-strategy.md` — how the project tests (defaults to **TDD**: tests are written from
   acceptance criteria before implementation). This guides `test-creator` and `implement`.
8. `docs/ui-conventions.md` — **if the project has any UI** — a thin starter set of frontend
   conventions (see Step 4.5). This is the UI source of truth that `implement`'s frontend lens
   reads, so UI stories come out consistent instead of ad hoc. Skip only for pure-backend projects.

## Procedure

### Step 1 — Understand the system from the PRD/epics
Read the PRD and epics. Extract: the major components implied by the feature areas, the
non-functional drivers (scale, latency, privacy, compliance), the deployment target, and any
constraints. Note what is MVP vs Later so the design isn't over-built for day one.

### Step 2 — Draft the proposal (Phase A)
Use `templates/tech-design-proposal.md.tmpl`. For each major decision, give 2-3 real options
with honest trade-offs and a recommendation. Cover at least:
- **Language(s) & framework(s)** — backend, frontend, and any AI/ML runtime the PRD implies.
- **Architecture style** — monolith vs services; for greenfield MVP, bias toward the simplest
  thing that meets the NFRs (often a modular monolith) and note when to split later.
- **Data stores** — primary DB, plus any specialised store the PRD requires.
- **Key libraries / external services** — only the ones the requirements actually demand.
- **Deployment** — honour the user's target (e.g. local Docker first), designed to extend.
- **Testing approach** — propose TDD unless the user objects; name the test runner per layer.
- **Repo shape** — monorepo vs multi-repo; service/package layout at a high level.

Pin **current** versions. If web search is available, verify the latest stable versions and
any recent breaking changes rather than relying on memory. State assumptions for anything the
user hasn't specified.

### Step 3 — Present and STOP for approval
Show the proposal. Explicitly list the decisions you need the user to confirm or change. Do
not proceed to Phase B until the user approves (in full or with edits). If they change a
choice, update the proposal and re-confirm. This gate is the point of the skill.

### Step 4 — Write the committed design (Phase B, only after approval)
Once approved, write the Phase B artifacts. Each locked decision becomes an ADR (context →
decision → consequences) so the reasoning survives. Derive `data-model.md` and
`api-contracts/` from the PRD's requirements so they're traceable. Write `testing-strategy.md`
making the TDD flow explicit: acceptance criteria → failing tests → implementation → green.
Write `architecture.md` with both prose and diagrams. Include whatever diagrams best explain
the system, using Mermaid in markdown so the architecture is easier to scan and keeps the
system behavior obvious to future implementers. Prefer at least:
- a component / container diagram for the major building blocks
- one or more sequence diagrams for the critical workflows
- a state diagram for meaningful lifecycle state machines
- a deployment / topology diagram when runtime layout matters
Add other diagrams when they clarify the design better than prose alone.

### Step 4.5 — Propose a thin UI conventions starter (if the project has any UI)
If the architecture includes a frontend, the project needs `docs/ui-conventions.md` — the UI
source of truth `implement`'s frontend lens reads. Most projects START with no design system, so
this skill PROPOSES a thin, sensible starter for the user to approve (same propose→approve gate),
covering the decisions that otherwise get made inconsistently per-story:
- **Component framework & structure** — the chosen frontend stack and how components are organized
  (mirroring the source-tree convention).
- **Design tokens (minimal)** — a starting spacing scale, color roles (primary/surface/text/
  error/etc.), and typography, so stories don't each invent their own values.
- **State & data-loading pattern** — where state lives (local vs shared store) and how API data is
  fetched/loaded, so every UI story follows one approach.
- **Required UI states** — loading / error / empty / success must all be handled.
- **Accessibility bar** — semantic HTML, labels, keyboard operability, contrast.
- **Responsive expectations** — the breakpoints to support.
Keep it THIN — a starter that creates baseline consistency, not an exhaustive design system. It is
designed to GROW: `design-sync` promotes patterns that UI stories establish into this file over
time, so an initially ad-hoc UI becomes systematic. Present it for approval alongside the rest of
Phase B; on approval write `docs/ui-conventions.md`.

### Step 5 — Hand off
Tell the user the committed design is ready and the next step is `project-scaffold`, which
turns this design into a runnable repository skeleton. Note that `project-scaffold` reads
`tech-stack.md`, `architecture.md`, and `testing-strategy.md` directly.

## Design principles for greenfield (keep it honest)
- **Right-size for the MVP.** Don't design the Stage-3 system on day one; design for MVP with
  clear seams for later. Over-engineering is the most common greenfield mistake.
- **Make NFRs concrete.** "Secure" / "scalable" must map to specific choices (auth method,
  encryption, expected load) or they won't be built.
- **Traceability.** Data model and contracts should trace back to PRD requirement IDs so the
  design provably covers the requirements.
- **One decision, one ADR.** If a choice is reversible and trivial, skip the ADR; if it shapes
  the codebase, record it.

## Amend mode (mid-build, triggered by story-design escalation)
The greenfield proposal above runs once. But the architecture is not frozen — when
`story-design` hits its escalation rule (a story needs a new integration, service, cross-cutting
concern, or something that contradicts an existing ADR), it kicks the change here rather than
letting the story bend the architecture silently. In amend mode:

1. **Scope the change.** Read the escalating story, its `story-design` note, and the current
   committed design. Identify exactly what the architecture lacks — keep the change minimal and
   targeted, not a redesign.
2. **Propose the amendment (still propose → approve).** Present options + a recommendation for
   just the delta: the new component/integration/boundary, its effect on the data model and
   contracts, and the consequences. The approval gate still applies — do not lock it unilaterally.
3. **On approval, amend the artifacts:**
   - Update `architecture.md` (and `data-model.md` / `api-contracts/` if affected) with the delta.
   - Add a **new ADR** recording the change: context (the story that forced it), decision,
     alternatives, consequences. Never edit an old ADR to pretend the decision was always there;
     supersede it if needed (mark the old one superseded, add a new one).
   - Note whether `project-scaffold` needs a one-off touch (e.g. a new service folder) — usually
     a small structural add, not a re-scaffold.
4. **Hand back to the loop.** Tell the user the design is amended and the escalated story can
   resume at `story-design` / `test-creator` against the updated architecture. The amendment will
   also surface to other in-progress stories via `design-sync` when it lands.

Amend mode is for genuine structural changes only. Incremental additions (a new endpoint or
field the architecture already accommodates) do NOT come here — they're handled by `design-sync`
at merge time. The dividing line is the same as `story-design`'s escalation rule.

## Reference files
- `templates/tech-design-proposal.md.tmpl` — the Phase A proposal structure.
- `templates/ADR.md.tmpl` — Architecture Decision Record format.
- `templates/architecture.md.tmpl` — committed architecture doc.
- `templates/ui-conventions.md.tmpl` — thin UI conventions starter (Step 4.5, projects with UI).
- `references/stack-selection-guide.md` — how to choose and justify, with the approval gate.
- `examples/` — a worked proposal + committed design for the Learn Portal MVP.
