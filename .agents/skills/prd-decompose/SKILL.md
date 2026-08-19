---
name: prd-decompose
description: >
  Decompose a Product Requirements Document (PRD) into well-formed Jira EPICS and create them
  in a Jira project using the connected Atlassian MCP. Use this as the SECOND step of the PDLC
  chain, right after the `prd` skill. Trigger this even when the user just says "break the PRD
  into epics", "decompose the PRD", "create the epics in Jira", "import epics", or "set up the
  Jira project from the PRD". This skill reads a PRD.md, groups requirements into epics along
  feature-area boundaries, and creates them via Atlassian MCP tools (it does NOT call the Jira
  REST API directly).
compatibility: >
  Tool-agnostic skill body (Claude Code or Codex). Requires the Atlassian (Jira) MCP server to
  be connected so the agent can search projects and create epic issues. If the MCP is not
  connected, the skill falls back to emitting a Jira-importable file the user can upload.
model: sonnet   # tier: standard (haiku=cheap, sonnet=standard, opus=strong)

---

# prd-decompose — PRD to Jira Epics

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


Second link in the chain. It turns a structured PRD into epics and puts them in Jira via the
**Atlassian MCP**. Epics are the unit of planning; getting their boundaries right is what
makes the later `user-story` step clean.

> Do not call the Jira REST API from a script here. Issue creation happens through the
> connected Atlassian MCP tools. This skill's job is to (a) produce correct epic content and
> (b) drive the MCP calls in the right order.

## Input it expects
A `PRD.md` produced by the `prd` skill (numbered FR/NFR requirements grouped under feature
areas FA-N). If given a free-form PRD, first normalise it into that shape before decomposing.

## Output it produces
1. `epics.md` — the proposed epics with their mapped requirements (the reviewable plan).
2. Created Jira epics in the target project (via Atlassian MCP).
3. `epic-map.json` — a machine-readable map of `{epic_key -> [requirement IDs]}` so the next
   skill (`user-story`) knows which requirements each epic must cover. Traceability lives here.

## Procedure

### Step 1 — Read and group
- Parse the PRD. Each **feature area (FA-N)** is the default epic boundary.
- Refine boundaries: if a feature area holds 15+ requirements or two clearly separable
  themes, split it into two epics. If two tiny areas are tightly coupled, consider merging.
- Keep MVP and Later requirements together under the same epic but note release on each;
  scope can be filtered later.
- Order the resulting epics to make dependency flow obvious when that helps readability.
  Foundational epics that unblock others should come earlier than dependent epics.
- Run a **lifecycle completeness** pass while grouping:
  - For every capability, ask what prerequisite actors, entities, or states it depends on.
  - For every prerequisite actor or entity, make its lifecycle explicit: created by another
    requirement, supplied externally, or intentionally out of scope.
  - For every persisted core entity or state, confirm which requirement creates it before
    other requirements consume it.
  - If a requirement assumes an existing actor, entity, or state and no requirement creates
    it, either add the missing requirement, split the epic, or document the assumption clearly.

### Step 2 — Write each epic (use `templates/epic.md.tmpl`)
Each epic needs:
- **Summary** — short, outcome-focused ("Curriculum ingestion & retrieval", not "FA-3").
- **Description** — the problem this epic solves, the domain context behind it, the primary
  users/actors, and the value it delivers. Write it so someone who has not read the PRD can
  still understand the feature area.
- **Functional scope** — the concrete capabilities and workflows the epic must deliver,
  including key inputs/outputs, integrations, data touched, and any notable edge cases or
  non-goals.
- **Implementation guidance / dependency order** — the recommended build sequence for the
  stories inside the epic. Call out foundation-first work, prerequisite data models or
  services, integration points, and which pieces should come before others when dependencies
  exist.
- **Goal / definition of done** — when is this epic complete.
- **Covered requirements** — the explicit list of FR/NFR IDs rolled into it (traceability).
- **Release** — MVP or Later (or "mixed", with the MVP subset listed).
- **Labels** — feature-area code + release, for filtering in Jira.

### Step 3 — Confirm the target Jira project
Before creating anything:
- Use the Atlassian MCP to **list/search accessible Jira projects**.
- Confirm the target project key with the user if it's not already specified (e.g. "LEARN").
- Confirm the epic issue type name in that project (often "Epic"; some projects rename it).

### Step 4 — Create the epics via Atlassian MCP
For each epic, call the MCP issue-creation tool with: project key, issue type = Epic,
summary, description (paste the epic body), and labels. After each creation:
- Capture the returned **issue key** (e.g. LEARN-12).
- Record it in `epic-map.json` against the requirement IDs that epic covers.
Create epics one at a time and verify each succeeds before continuing; do not fire a batch
blindly. If a creation fails, stop, report which epic failed and why, and let the user decide.

### Step 5 — Hand off
Write `epics.md` and `epic-map.json`. Tell the user: N epics created in project <KEY>, here
are the keys, and the next step is the `user-story` skill to break each epic into stories
with acceptance criteria.

## If the Atlassian MCP is not connected
Fall back gracefully: produce `jira-epics-import.csv` (columns: Summary, Issue Type,
Description, Labels) that the user can import via Jira's CSV importer, and still write
`epics.md` / `epic-map.json`. Tell the user the MCP wasn't available and how to import.

## Boundary heuristics (quick reference)
- One feature area → usually one epic.
- An epic should be deliverable in a few sprints; if it's clearly a quarter of work, split.
- Cross-cutting NFRs (security, compliance, accessibility) that touch many areas can become
  their own epic (e.g. "Compliance & data protection") rather than being scattered.
- Every requirement must land in exactly one epic. No orphans, no duplicates — check the map.
- Epic descriptions should be self-contained: if a Jira reader sees only the epic issue, they
  should still understand the feature, the users, the implementation shape, and the order in
  which it should be built.
- If the epic has an internal dependency chain, document the preferred implementation order in
  the epic body rather than leaving it implicit.
- If an epic contains a flow that depends on an actor, entity, or state already existing, make
  that dependency explicit in the epic body and/or split out the prerequisite story instead of
  burying it inside another requirement.

## Reference files
- `templates/epic.md.tmpl` — epic structure.
- `templates/epic-map.schema.json` — the shape of the traceability map.
- `references/atlassian-mcp-usage.md` — how to drive the MCP tools (project lookup, create).
- `examples/` — epics decomposed from the sample PRD, with the resulting map.
