---
name: design-sync
description: >
  Keep the system design references and AGENTS.md continuously in sync with shipped code: when a
  story is ready to merge, reconcile what it changed against architecture.md, data-model.md,
  api-contracts/, and AGENTS.md, commit the doc updates onto the SAME branch so they ride with
  the merge, and flag any in-progress stories the change affects. Use this at the end of the
  per-story loop, inside pr-create's flow, BEFORE the PR merges. Trigger this even when the user
  just says "sync the design", "update the architecture docs", "update AGENTS.md", "reconcile
  the design after this story", or when a story PR is about to merge. This skill updates
  documentation/reference files only — it does NOT change production code or tests, and it is
  proportionate (a no-op when the story changed nothing at the system level).
compatibility: >
  Tool-agnostic (Claude Code or Codex). Reads the merged story's diff, the committed design from
  tech-design, and AGENTS.md. Commits doc updates to the story branch via git/GitHub MCP. Uses
  the Atlassian MCP to find in-progress stories to flag. Runs in the local Docker dev environment.
model: haiku   # tier: cheap (haiku=cheap, sonnet=standard, opus=strong)

---

# design-sync — Keep the Design & AGENTS.md Current (per merge)

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


The anti-drift step. In a multi-developer project, people start `story-design` by reading
`architecture.md`, `data-model.md`, and `AGENTS.md` to decide their "how". If those references
lag behind merged code, parallel stories design against a stale picture and collide. This skill
runs at each story's merge to reconcile the references with what actually shipped — so the next
person to read them sees reality.

> Documentation only. This skill never touches production code or tests. Its job is to make the
> design references match the code that's merging, and to warn anyone whose in-progress work the
> change affects.

## Why per-merge (not per-epic)
With multiple stories in flight, the references must reflect reality continuously — at any moment
someone else is reading them to make a design decision. Per-epic batching would leave them stale
for a whole epic, exactly when parallel stories most overlap. So this runs every merge, but is
**proportionate**: most merges change nothing at the system level and the sync is a near no-op.

## Where it runs
Inside `pr-create`'s flow, **before the PR is opened**, while still on the story branch — so the
doc commit becomes part of the PR and merges atomically with the code (it rides with the merge,
not a follow-up PR). Order in the loop:
`... test-verify → code-review → pr-create [ design-sync commits docs to branch → open PR → … → merge ]`.

## Input it expects
- The story's merged diff (the branch `feat/<STORY_KEY>-...` against main).
- The committed design from `tech-design`: `architecture.md`, `data-model.md`, `api-contracts/`,
  `adr/`, and the repo's `AGENTS.md`.
- Access to Jira (Atlassian MCP) to list in-progress stories for overlap-flagging.

## Output it produces
- Updated reference files (only those that actually changed), committed to the story branch:
  potentially `architecture.md`, `data-model.md`, `api-contracts/`, `AGENTS.md`, and a running
  `docs/CHANGELOG-design.md`.
- A `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/design-sync.md` note: what changed in the references (or "no system-level
  change"), and a list of affected in-progress stories flagged for design re-check.

## Procedure

### Step 1 — Diff the story against the current design
Compare what the story added/changed (new endpoints, new/changed data fields, new module
dependencies, new external integrations, config) against the current reference docs. Classify:
- **No system-level change** — behavior the design already anticipated. Common case.
- **Incremental change** — a new endpoint/field/module-detail the docs should now reflect.
- **Structural change** — should have come through a `tech-design` amendment already; if it
  didn't, flag it (the escalation rule in story-design was bypassed).

### Step 2 — Update only what changed (proportionate, idempotent)
- **No system-level change** → append a one-line entry to `docs/CHANGELOG-design.md` (so nothing
  is lost) and stop. Do NOT churn the architecture narrative. A near no-op is the correct outcome.
- **Incremental change** → update the specific sections: add the endpoint to the contracts /
  architecture's current-endpoints list, add the field to `data-model.md`, update the module
  description. Touch only the lines that changed.
- **New UI pattern (frontend/full-stack stories)** → if the merged story established a reusable UI
  pattern not yet in `docs/ui-conventions.md` (a new component type, a token, a state/loading
  approach, an accessibility decision), PROMOTE it into `ui-conventions.md` so the next UI story
  reuses it instead of reinventing. This is how the initially-thin conventions grow into a real
  design system. Only promote genuinely reusable patterns — not story-specific one-offs.
- Always reconcile against the CURRENT state of main, not a stale snapshot — so concurrent merges
  converge instead of clobbering each other (last-write-aware, idempotent).

### Step 3 — Update AGENTS.md (first-class output)
`AGENTS.md` is what every future story's agent reads first, so keep it fresh. Update, as needed:
- module map / responsibilities (if a module gained meaningful behavior),
- current endpoints and data entities (summary level, not full contracts),
- conventions or commands if the story introduced any,
- a pointer to the design changelog for detail.
Keep it concise and current — it's a living orientation file, not a full spec.

### Step 4 — Flag affected in-progress stories (the multi-user safeguard)
Via the Atlassian MCP, list stories that are In Progress / In Review (not this one). For each,
check whether this change touches the same module, endpoint, or data entity their `story-design`
references. For any overlap:
- add a comment on that Jira story: "Design changed by <STORY_KEY> (e.g. `material` table gained
  `checksum`); please re-check your story-design before/again implementing,"
- list them in `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/design-sync.md`.
This is a notification, not a block — it lets the other developer re-align proactively instead of
discovering the collision at merge.

### Step 5 — Commit onto the story branch (before the PR opens)
Commit the doc/reference changes to the SAME branch as the story (message:
`<STORY_KEY>: design-sync — update references`), while still on the branch and before `pr-create`
opens the PR. This way the doc updates are part of the PR and merge atomically with the code. Do
not open a separate PR; do not commit straight to main.

### Step 6 — Hand back to pr-create
Report what was synced (or that it was a no-op) and which stories were flagged. `pr-create` then
opens the PR (now including the doc updates) and transitions Jira. If a structural change was
detected that never went through a `tech-design` amendment, recommend pausing to do that first.

## Boundaries
- Docs/reference files only — never production code or tests.
- Not a design *decision* maker — that's `tech-design` (amend mode). This reflects decisions
  already shipped; it escalates if it finds an un-amended structural change.
- Does not merge or block — it commits docs to the branch and flags overlaps; `pr-create` owns
  the merge, humans own the response to flags.

## Reference files
- `templates/design-sync-note.md.tmpl` — the per-merge sync note + flagged stories.
- `templates/AGENTS.md.tmpl` — the structure of the living AGENTS.md this keeps current.
- `references/sync-decision-guide.md` — classifying no-op vs incremental vs structural; idempotency.
- `examples/` — a worked sync for story LEARN-21 (incremental: new endpoint + table).
