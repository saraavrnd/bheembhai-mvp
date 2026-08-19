---
name: epic-review
description: >
  Interrogate an already-decomposed epic's FULL set of stories together — not one at a time —
  to surface gaps that only show up across siblings: the same actor/role assumed by several
  stories but never actually defined, a requirement claimed by two stories in contradictory
  ways (or claimed by none), a cross-epic dependency a story quietly assumes but never states,
  a scope hole no single story's "out of scope" admits to. Rewrites the affected stories once,
  together, instead of asking the same question in every per-story review. Trigger this when
  the user says "review this epic", "drill into this epic", "find gaps across these stories",
  "sanity check the epic", "is this epic ready for story-design", "these stories feel
  inconsistent", or names an epic key with a request to clarify/tighten it. On-demand utility
  skill — run it right after `user-story` finishes an epic (before `epic-sequence` or any
  per-story `story-review`), or any time later on an epic that already feels shaky.
compatibility: >
  Tool-agnostic (Claude Code or Codex). Reads architecture.md / data-model.md / api-contracts /
  ui-conventions.md / ADRs from the committed tech-design, the PRD's feature-area requirements,
  and every sibling story in the epic. Atlassian MCP optional — used to fetch and update (never
  create) the Jira epic and its child stories if keys are given; falls back to working on
  `stories.md` directly if the MCP isn't connected.
model: opus   # tier: strong (haiku=cheap, sonnet=standard, opus=strong)

---

# epic-review — Drill a Whole Epic for Cross-Story Gaps

> **Artifact paths:** Resolve all project-artifact locations (architecture, data model, PRD,
> epics, stories, ADRs, API contracts, per-story notes, etc.) from the **Project Layout** table
> in `AGENTS.md` at the repo root — see `_SHARED-project-layout.md`. Do not hardcode paths; the
> current layout places these under `docs/` (e.g. `docs/architecture.md`, `docs/adr/`,
> `docs/api-contracts/`, `docs/product/`). **Epic-level artifacts live in that epic's own
> `_epic/` folder** `docs/product/epics/<EPIC_KEY>/_epic/` (stories.md, story-map.json,
> epic-sequence.{md,json}, **epic-review.md**); per-story artifacts stay in
> `docs/product/epics/<EPIC_KEY>/stories/<STORY_KEY>/`. This skill's own `templates/`,
> `references/`, `examples/` stay inside the skill folder and never move under `docs/`.

> **Cost & context:** This skill runs at the `model:` tier in its frontmatter (see
> `_SHARED-cost-and-models.md`). An epic can hold a dozen-plus stories — batch-read `stories.md`
> and `story-map.json` once rather than fetching per story, load architecture/data-model slices
> only for the modules the epic's stories collectively touch (not whole files, not other
> epics' modules), and for any MCP calls request minimal fields and batch reads. MCP results
> persist for the session — keep them small, this skill's blast radius is already wider than
> `story-review`'s.

`story-review` drills one story at a time — it's precise but blind to the epic around it. Some
gaps only exist in that blind spot: an actor named "admin" that three stories each assume is
defined, when the data model actually has no such role; a requirement two stories both claim to
cover, worded so each would build a different, contradicting interpretation; a story that leans on
a concept another epic owns without ever writing that dependency down. Left uncaught, every
affected story's `story-design` (or a separate `story-review` pass) re-discovers the same gap
independently and asks the user the same question N times. This skill runs the drill once, across
the whole epic, and rewrites every affected story together.

## Input it expects
- An epic — by Jira key (fetched via Atlassian MCP) or its existing `stories.md` /
  `story-map.json` pair on disk.
- The committed design: `architecture.md`, `data-model.md`, `api-contracts/`, ADRs, and
  `ui-conventions.md` for feature areas with frontend/full-stack stories.
- The PRD's feature-area section and the FR/NFR IDs `epic-map.json` assigns to this epic.
- Other epics' `epic-map.json` entries and `epic-sequence` position, only far enough to tell
  whether something this epic's stories assume is legitimately another epic's deliverable.

## Output it produces
1. Every affected story's acceptance criteria, story statement, "out of scope", and
   Notes/dependencies — rewritten in place in
   `docs/product/epics/<EPIC_KEY>/_epic/stories.md` — gaps closed with the user's answers. One
   answer may touch several stories; all of them get updated in the same pass.
2. `epic-review.md` written to `docs/product/epics/<EPIC_KEY>/_epic/epic-review.md` (from
   `templates/epic-review.md.tmpl`) — the audit trail: which stories were loaded, what
   cross-story gaps were found, what was asked, what was answered, which stories changed.
3. If Jira keys were given: the epic's description and each touched story's
   description/acceptance-criteria field updated to match (via Atlassian MCP) — existing
   issues only, never new ones.
4. If `epic-map.json` or `story-map.json` coverage changed as a result, both updated to match.
5. If a gap turns out to be a genuine architecture conflict (not a cross-epic sequencing
   assumption that's already accounted for): a flagged "Architecture impact" note recommending
   `tech-design` (amend mode) before any of the epic's stories reach `story-design`.

## Procedure

### Step 1 — Resolve the epic and load its full story set
Get the epic (Jira key via MCP, minimal fields: summary, description, goal, child-story links —
or resolve from disk). Load `stories.md` and `story-map.json` for this epic in one read each —
every story in the epic, not a sample. This full set is the whole point: gaps here are defined by
comparing stories against each other, not against one story's own text.

### Step 2 — Load only the system context the epic collectively touches
Read the PRD's feature-area section and the exact FR/NFR IDs `epic-map.json` maps to this epic.
Read `architecture.md` / `data-model.md` slices for every module the epic's stories reference
(union across stories, still slices — not whole files, and not modules only some *other* epic
touches). Read `api-contracts/` sections implied by any story. Skim ADRs by keyword. Check
`epic-sequence.md`/`.json` and other epics' `epic-map.json` entries only far enough to tell
whether something a story assumes is legitimately another epic's planned deliverable and how
that epic is sequenced relative to this one.

### Step 3 — Run the epic-level drill checklist
Work through `references/gap-checklist.md`'s categories, each scoped to what's only visible
*across* stories: actor/role/entity consistency, requirement coverage completeness and
non-duplication, epic-wide scope boundary, cross-story (and cross-epic) architecture fit,
dependency-note sanity, epic-wide silent NFRs, and epic-level traceability. Explicitly skip
anything that is a single-story concern with no cross-story angle (one vague adjective in one
AC, one missing unhappy path with no sibling implication) — that stays `story-review`'s job; do
not duplicate it here. Only gaps that involve two or more stories, or the epic's own boundary,
earn a question.

### Step 4 — Interview (the drill)
Turn each real cross-cutting gap into one specific, answerable question, naming which stories it
touches. Ask the highest-impact few first — an undefined actor used by half the epic's stories,
or a silently-assumed cross-epic dependency, outranks a wording nitpick. If an answer reveals a
new gap in a story not yet considered, follow up before moving on. If a gap is a genuine
architecture conflict (not a legitimate, already-sequenced cross-epic dependency — see the
checklist's category 4 for the distinction), do not resolve it here; hold it for Step 6's
escalation note.

### Step 5 — Rewrite and present for approval
By this point the rewritten stories and `epic-review.md` must already exist — do the work, then
show it. Rewrite every affected story's statement/acceptance-criteria/out-of-scope/notes in the
same shape `user-story` produces. Show a clear before/after per touched story (or the resolved-
gaps log) and ask the user to approve or push back. If they change an answer, update every story
it touches and re-confirm.

> Anti-stall note: read the inputs and produce the rewrite and the log in the same turn the
> interview concludes, THEN pause. Don't narrate that you're "about to check the architecture"
> and stop there.

### Step 6 — On approval, write back
- Update every touched story's section in `docs/product/epics/<EPIC_KEY>/_epic/stories.md` in
  place, in one pass.
- Write `epic-review.md` to `docs/product/epics/<EPIC_KEY>/_epic/epic-review.md` (from
  `templates/epic-review.md.tmpl`).
- If coverage changed (a gap revealed a requirement no story covers, or resolved a duplicate
  claim), update `story-map.json` and, if the epic's own requirement set changed, `epic-map.json`.
- If Jira keys were given, update the epic's description and every touched story's
  description/acceptance-criteria via the Atlassian MCP — see
  `references/atlassian-mcp-usage.md`. Never create a new issue here.
- If Step 4 held an architecture-conflict gap, state it plainly now and recommend running
  `tech-design` (amend mode) before any affected story starts `story-design` — catching it once
  here is cheaper than each story's own `story-design` escalation rediscovering it separately.
- Tell the user which stories are ready for `story-review` (if any still feel individually
  underspecified) or straight to `story-design` — a clean epic is a valid, useful outcome too.

## What this skill is NOT
- Not a replacement for `story-review` — that skill still does the deep single-story drill
  (vague thresholds, one story's missing unhappy path). This only catches what requires
  comparing stories to each other or to the epic's own boundary.
- Not `epic-sequence` — that skill computes build ORDER (topological waves, Jira "blocks"
  links). This skill only flags a dependency note that's missing, contradictory, or points at a
  story that doesn't exist; it never computes or writes the order itself.
- Not authority to change architecture — a real architecture-conflict gap gets flagged to
  `tech-design`, never quietly resolved by inventing a design inside this interview.
- Not automatic — on-demand. Reach for it right after `user-story` finishes an epic (cheapest
  point, before any per-story work starts), or any time an epic's stories feel like they're
  talking past each other.

## Reference files
- `references/gap-checklist.md` — the epic-scoped drill checklist by category, with what a real
  (vs. nitpick, vs. already-`story-review`'s-job) gap looks like in each.
- `references/atlassian-mcp-usage.md` — fetching and **updating** (not creating) a Jira epic and
  its child stories, batched.
- `templates/epic-review.md.tmpl` — the review-log artifact structure.
- `examples/epic-review-BEEM-3.md` — an illustrative worked pass over this repo's own `BEEM-3`
  epic (Project access, identity & run ownership).
