---
name: user-story
description: >
  Break a Jira EPIC into well-formed USER STORIES, each with testable acceptance criteria, and
  create them in Jira under their parent epic using the connected Atlassian MCP. Use this as the
  THIRD step of the PDLC chain, after `prd-decompose`. Trigger this even when the user just says
  "write the user stories", "break the epic into stories", "create stories with acceptance
  criteria", "generate Jira stories", or names an epic to expand. This skill turns the
  requirements mapped to an epic (via epic-map.json) into stories whose acceptance criteria are
  written so they can later drive the `test-creator` and `implement` skills.
compatibility: >
  Tool-agnostic skill body (Claude Code or Codex). Requires the Atlassian (Jira) MCP to create
  and parent stories. Falls back to a Jira-importable file if the MCP is not connected.
model: sonnet   # tier: standard (haiku=cheap, sonnet=standard, opus=strong)

---

# user-story — Epic to Stories with Acceptance Criteria

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


Third link in the chain. It converts an epic (and the PRD requirements mapped to it) into
INVEST-quality user stories, each with acceptance criteria precise enough that a test can be
written from them directly. These acceptance criteria are the contract that the downstream
`implement` and `test-creator` skills build against — so vagueness here costs the most later.

## Input it expects
- An epic — either a Jira epic key (e.g. LEARN-12) the agent can fetch via the Atlassian MCP,
  or the rendered epic body.
- `epic-map.json` from `prd-decompose` — to know which PRD requirement IDs this epic must
  cover (the source of truth for completeness).
- Optionally the original `PRD.md` for requirement wording.

## Output it produces
1. `docs/product/epics/<EPIC_KEY>/_epic/stories.md` — the reviewable set of stories with acceptance criteria.
2. Created Jira stories under the epic (via Atlassian MCP), each linked to its parent.
3. `docs/product/epics/<EPIC_KEY>/_epic/story-map.json` — `{story_key -> [requirement IDs]}`, extending traceability
   from epic → story → requirement. Every requirement mapped to this epic must be covered by
   at least one story.

## Procedure

### Step 1 — Gather the epic's requirements
From `epic-map.json`, get the FR/NFR IDs this epic covers. Pull their exact wording from the
PRD. These define what the stories must collectively satisfy.

Before slicing, run a **story-readiness** check:
- If a story would assume an actor/entity/state exists, verify another requirement or story
  creates it first.
- If the epic leaves a prerequisite lifecycle ambiguous, stop and send that ambiguity back to
  `prd-decompose` or `prd` rather than hiding it inside a story.
- If the epic mixes prerequisite creation and usage in one requirement, split them into separate
  stories unless the combined slice is still a single testable user-visible outcome.

### Step 2 — Slice into stories
- Each story is **one coherent slice of user-visible value** — vertical, not a technical layer.
  ("As a teacher I can upload a course PDF and see it accepted" — not "build the upload
  endpoint".)
- A single requirement may need one story or several; several small requirements may combine
  into one story. Optimise for INVEST (Independent, Negotiable, Valuable, Estimable, Small,
  Testable), with **Small and Testable** mattering most for the AI build chain.
- Split a story if it has more than ~5-7 acceptance criteria or covers two user goals.

### Step 3 — Write each story (use `templates/story.md.tmpl`)
Each story needs:
- **Title** — short, action-oriented.
- **Story statement** — "As a <role>, I want <capability>, so that <benefit>."
- **Acceptance criteria** — in **Gherkin** (Given/When/Then), one scenario per distinct
  behaviour, including the unhappy paths (invalid input, permission denied, empty state).
  Each scenario must be a concrete pass/fail check.
  For stories that create or change persistent platform state, phrase the criteria against the
  durable system of record the architecture names, not against a temporary shim or mockable
  placeholder. The downstream design and tests should be able to prove the real row/state exists.
- **Covered requirements** — the FR/NFR IDs this story satisfies (traceability).
- **Out of scope** — what this story explicitly does NOT do (prevents scope creep into tests).
- **Notes / dependencies** — sequencing, data needs, design links.
- **Estimate** — story points if known.

### Step 4 — Verify coverage before creating
Cross-check the stories against the epic's requirement IDs. Every requirement must be covered
by at least one story's acceptance criteria. If any requirement is uncovered, add or extend a
story. Report the coverage summary to the user.

### Step 5 — Create stories in Jira via Atlassian MCP
For each story (see `references/atlassian-mcp-usage.md`):
- Create issue: project key, type = Story, summary, description (story statement + acceptance
  criteria + out-of-scope), labels.
- **Parent it to the epic** (parent field or Epic Link depending on project type).
- Set story points if the field exists.
- Capture the returned story key into `docs/product/epics/<EPIC_KEY>/_epic/story-map.json`.
Create one at a time, verify each, record the key immediately. Search first to avoid creating
duplicates if the skill is re-run.

### Step 6 — Hand off
Tell the user: N stories created under <EPIC_KEY>, coverage of requirements is complete (or
flag gaps), keys listed, and the next steps are `implement` (story → code) and `test-creator`
(acceptance criteria → tests).

## Writing acceptance criteria that drive tests (the core rule)
Each Given/When/Then must be executable as a test with no further interpretation.

- Weak: "Then the upload works correctly."
- Strong:
  - Given a signed-in teacher, When they upload a 10 MB PDF, Then the file is accepted and its
    status shows "processing" within 2 seconds.
  - Given a signed-in teacher, When they upload a 40 MB file, Then it is rejected with the
    message "File exceeds 25 MB limit" and nothing is stored.

If a criterion can't be turned into a test without guessing, rewrite it.

If a story would require a missing prerequisite actor, entity, or state that no story in the
epic creates, do not paper over it with an assumption; flag the gap and route it back to
epic/PRD review.

## If the Atlassian MCP is not connected
Fall back to `jira-stories-<EPIC_KEY>-import.csv` (Summary, Issue Type, Description, Parent/
Epic Link, Labels) and tell the user. Still write the markdown and the story map.

## Reference files
- `templates/story.md.tmpl` — story + acceptance-criteria structure.
- `references/acceptance-criteria-guide.md` — Gherkin patterns and the testability bar.
- `references/atlassian-mcp-usage.md` — driving the MCP (shared with prd-decompose).
- `examples/` — stories with acceptance criteria derived from a sample epic.
