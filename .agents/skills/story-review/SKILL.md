---
name: story-review
description: >
  Interrogate a single, already-written user story against the system architecture, data model,
  API contracts, and sibling stories to surface gaps, ambiguity, and undecided edge cases —
  then rewrite the story's acceptance criteria to close them, with the user's sign-off, before
  a developer (or `story-design`) ever picks it up. Trigger this when the user says "review this
  story", "drill into this story", "find gaps in this story", "sharpen/tighten this story",
  "grill this story", "is this story ready for dev", "this story feels ambiguous", or names a
  story key with a request to clarify/clean it up. On-demand utility skill — not an automatic
  stage of the per-story loop; run it whenever a story (new or old) feels underspecified.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Reads architecture.md / data-model.md / api-contracts /
  ui-conventions.md / ADRs from the committed tech-design, plus sibling stories in the same epic.
  Atlassian MCP optional — used to fetch and update (never create) the Jira story if a key is
  given; falls back to working on pasted story text if the MCP isn't connected.
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# story-review — Drill a Story for Gaps (interview → rewrite → approve)

> **Artifact paths:** Resolve all project-artifact locations (architecture, data model, PRD,
> epics, stories, ADRs, API contracts, per-story notes, etc.) from the **Project Layout** table
> in `AGENTS.md` at the repo root — see `_SHARED-project-layout.md`. Do not hardcode paths; the
> current layout places these under `docs/` (e.g. `docs/architecture.md`, `docs/adr/`,
> `docs/api-contracts/`, `docs/product/`). **Per-story artifacts live in that story's own
> folder** `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/` with short, un-suffixed
> filenames (story-design.md, test-plan.md, verification.md, code-review.md, design-sync.md,
> **story-review.md**); epic artifacts in `docs/product/epics/<EPIC_KEY>/_epic/`. Resolve a
> story's files under its folder only. This skill's own `templates/`, `references/`,
> `examples/` stay inside the skill folder and never move under `docs/`.

> **Cost & context:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). To avoid context bloat: load only the slices of artifacts you
> need (not whole files), don't re-read what's already in context, and for any MCP calls request
> minimal fields and batch reads rather than fetching repeatedly. MCP results persist for the
> session — keep them small.

A user story reads as complete and is still full of silent assumptions — an actor that isn't
quite defined, a precondition that assumes something another story hasn't built yet, an error
path nobody specified, a value the architecture can't actually support. Those gaps are cheapest
to catch here, on the story itself, before `story-design` commits to an interface built on top of
a guess. This skill runs a **grounded interview**: it doesn't ask generic "any ambiguity?"
questions, it reads the actual architecture/data-model/sibling-stories and asks pointed questions
only where something concrete doesn't line up — then rewrites the acceptance criteria with the
user's answers and gets sign-off before anything downstream consumes it.

## Input it expects
- A user story — by Jira key (fetched via Atlassian MCP) or pasted story text (statement +
  acceptance criteria), typically one produced by `user-story`.
- The committed design: `architecture.md`, `data-model.md`, `api-contracts/`, ADRs, and
  `ui-conventions.md` for frontend/full-stack stories.
- The epic's sibling stories (`stories.md` / `story-map.json`) for overlap and sequencing context.

## Output it produces
1. The story's acceptance criteria, story statement, and "out of scope" rewritten in place in
   `docs/product/epics/<EPIC_KEY>/_epic/stories.md`, gaps closed with the user's answers.
2. `story-review.md` written to **this story's folder**,
   `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/story-review.md` (create the folder if it
   doesn't exist; resolve `<EPIC_KEY>` via `docs/product/epic-map.json` if you only have the
   story key) — from `templates/story-review.md.tmpl`. This is the audit trail: which gaps were
   found, what was asked, what was answered, what changed.
3. If a Jira key was given, the Jira story's description/acceptance-criteria is updated to match
   (via Atlassian MCP) — the existing issue, not a new one.
4. If a gap turns out to be an architecture conflict rather than a story ambiguity: a flagged
   "Architecture impact" note recommending `tech-design` (amend mode) run before `story-design`.

## Procedure

### Step 1 — Resolve the story and its neighborhood
Get the story (Jira key via MCP, minimal fields: summary, description, acceptance-criteria field,
status, parent epic — or the pasted text). If only the story key is known, resolve its epic via
`epic-map.json`. Locate the story's entry in the epic's `stories.md` and its per-story folder
(create the folder if this is the first artifact for it). Load the epic's `story-map.json` and
the other stories in `stories.md` for later overlap checks — one batch read, not one fetch per
sibling.

### Step 2 — Load only the system context this story touches
Read `architecture.md` and `data-model.md` for the module(s)/entities the story's language
implies. Read `api-contracts/` if the story implies an existing or new endpoint. Read
`ui-conventions.md` if the story reads as frontend or full-stack. Skim relevant ADRs by keyword
rather than the whole `adr/` folder. Read slices, not whole files.

### Step 3 — Run the drill checklist
Work through `references/gap-checklist.md`'s categories against this specific story: actor/
precondition clarity, acceptance-criteria completeness (happy path only?), concreteness of
outcomes (vague thresholds/messages), architecture/data-model fit, silent NFRs, traceability to
FR/NFR IDs, overlap with sibling stories, and whether "out of scope" actually bounds the story.
Discard anything the story already answers unambiguously — only real gaps make it to Step 4.
Rank what remains by how much it would mislead `test-creator` if left unresolved.

### Step 4 — Interview (the drill)
Turn each real gap into one specific, answerable question — never "anything else unclear?".
Ask the highest-impact few first rather than dumping the whole list; if an answer reveals a new
gap, follow up before moving on. Keep it a conversation, not a form. If a gap turns out to be an
architecture conflict (the story needs an entity/endpoint/integration that doesn't exist and
isn't already someone else's in-flight work), do **not** try to resolve it here — surface it
plainly and hold it for Step 6's escalation note instead of pretending an interview answer can
invent architecture.

### Step 5 — Rewrite and present for approval
By this point the rewritten story and the `story-review.md` log must already exist — do the
work, then show it, rather than describing what you're about to check and waiting. Rewrite the
story statement, acceptance criteria (Gherkin, edge cases folded in), and "out of scope" using
the same shape `user-story` produces (`user-story/templates/story.md.tmpl`). Show a clear
before/after (or the full rewritten story plus the resolved-gaps log) and ask the user to approve
or push back. If they change an answer, update both the story and the log and re-confirm — this
realignment is the point of the step.

> Anti-stall note: read the inputs and produce the rewrite and the log in the same turn the
> interview concludes, THEN pause. Don't narrate that you're "about to check the architecture"
> and stop there.

### Step 6 — On approval, write back
- Update the story's section in `docs/product/epics/<EPIC_KEY>/_epic/stories.md` in place.
- Write `story-review.md` to the story's folder (from `templates/story-review.md.tmpl`).
- If a requirement's coverage changed (a gap revealed a new FR/NFR this story now covers, or
  that it doesn't), update `story-map.json` accordingly.
- If the story came from Jira, update the existing issue's description/acceptance-criteria field
  via the Atlassian MCP — see `references/atlassian-mcp-usage.md`. Never create a new issue here.
- If Step 4 held an architecture-conflict gap, state it plainly now and recommend running
  `tech-design` (amend mode) before `story-design` starts on this story — this is the earliest,
  cheapest point that gap could be caught, even earlier than `story-design`'s own escalation
  check.
- Tell the user the story is ready for `story-design` (or, if nothing changed, say so — a clean
  story is a valid, useful outcome of this skill too).

## What this skill is NOT
- Not the "how" — it doesn't decide interfaces, endpoints, or data-model deltas; that's
  `story-design`. This only sharpens the "what" the story is asking for.
- Not authority to change architecture — a real architecture-conflict gap gets flagged to
  `tech-design`, never quietly resolved by inventing a design inside this interview.
- Not a duplicate of `story-design`'s escalation check — that one verifies fit once the how is
  decided; this one catches ambiguity earlier and cheaper, before design work starts.
- Not automatic — this is an on-demand utility skill, not a numbered stage every story must pass
  through. Reach for it when a story feels underspecified: right after `user-story`, before
  `story-design`, or on an older backlog story nobody's touched.

## Reference files
- `references/gap-checklist.md` — the full drill checklist by category, with what a real (vs.
  nitpick) gap looks like in each.
- `references/atlassian-mcp-usage.md` — fetching and **updating** (not creating) a Jira story.
- `templates/story-review.md.tmpl` — the review-log artifact structure.
- `examples/story-review-LEARN-21.md` — a worked pass over `user-story`'s LEARN-21 example.
