# Drill Checklist — What Counts as a Real Cross-Story Gap

A real gap here is something visible only when you hold two or more stories (or a story and the
epic's own boundary) next to each other — not something a single story's own text already
resolves. If `story-review`, reading this one story in isolation, would already have everything
it needs, it's not this skill's gap to raise.

## 1. Cross-story actor & role consistency
- Collect every actor name used across the epic's stories ("admin", "teacher", "project admin",
  "run user"). For each, check `data-model.md` / `architecture.md`: does a single, consistently-
  named concept back it, or is it prose shorthand with no backing entity/role/enum value?
- Where two stories use different words for what looks like the same actor, or the same word for
  what turns out to be two different actors (a platform-wide role vs. a project-scoped one),
  that's a real gap — a wrong guess here propagates into every story that uses the term.

**Not a gap:** the actor is used consistently by every story that mentions it, and it maps
cleanly to one entity/role/enum value already in the data model.

## 2. Requirement coverage completeness & non-duplication
- For every FR/NFR ID `epic-map.json` assigns to this epic, confirm at least one story's
  acceptance criteria actually exercises it (not just an unverified bullet under "Covered
  requirements").
- Where two stories both claim the same FR/NFR, check they cover genuinely distinct, non-
  contradicting slices of it (e.g., one is the API behavior, one is the browser UI over it) —
  and that neither one's acceptance criteria silently redefines a concept the other already
  defined (a status value, an error condition, a threshold).

**Not a gap:** two stories share an FR because the epic deliberately splits API vs. UI (or similar
non-overlapping slices) and their acceptance criteria don't contradict each other.

## 3. Epic-wide scope boundary
- Read every story's "Out of scope" together. Does the union leave a capability the FR/NFR text
  implies but that no story's acceptance criteria covers and no story's out-of-scope even
  mentions — a hole nobody owns?
- Does any story's "in scope" quietly overlap another's in a way that could be built two
  different, conflicting ways by two separate implementers?

**Not a gap:** a capability is explicitly and correctly marked "Later" release, or explicitly
owned by a named sibling story or a named other epic.

## 4. Cross-story / cross-epic architecture fit
- Does the epic's cumulative behavior need an entity, field, endpoint, or integration that isn't
  in `architecture.md` / `data-model.md` / `api-contracts/`?
- If it's missing, check whether it's legitimately another epic's planned deliverable (check that
  epic's `epic-map.json` entry and its `epic-sequence` position relative to this one) before
  calling it a conflict.
  - **If it IS another epic's legitimate deliverable, sequenced appropriately, and the depending
    story's own Notes/dependencies already say so:** not a gap.
  - **If it IS another epic's legitimate deliverable but the depending story's Notes/dependencies
    stay silent about it:** that silence is the gap — flag it and get the dependency written
    down, even though no architecture change is needed.
  - **If it needs something that doesn't exist and isn't credibly any epic's planned
    deliverable:** that's a genuine architecture-conflict gap — hold it for the Step 6
    escalation note, don't resolve it by inventing design here.

**Not a gap:** the story references something already sketched in `data-model.md` as planned but
not yet built by an earlier story in the same epic — normal in-epic sequencing.

## 5. Dependency-note sanity (not ordering)
- For every "Depends on X" / "prerequisite for Y" note in `stories.md`, confirm story X actually
  exists in this epic (or is named correctly if it's in another epic).
- Spot-check reciprocity: if story A's notes say "B depends on me," does B's own notes section
  agree? A one-sided dependency claim is a real gap — someone will build B assuming A isn't
  needed yet.
- This is a sanity check, not order computation — don't propose a build sequence or wave; that's
  `epic-sequence`'s job. Flag the mismatch and let the user reconcile it.

**Not a gap:** dependency notes are consistent and refer to real stories; the actual build order
is `epic-sequence`'s concern, not this skill's.

## 6. Epic-wide silent NFRs
Does an NFR the PRD raises for this epic's feature area plausibly apply to *every* story in the
epic uniformly (audit logging on every mutating action, accessibility on every screen, a
performance budget on every endpoint), and none of the stories mention it? Only ask if a real NFR
row in the PRD grounds the question — don't invent one the PRD never raised, and don't re-ask a
silent NFR that's a single story's own concern with no epic-wide angle (that's `story-review`'s
job).

**Not a gap:** the NFR is scoped to a different feature area, or is already addressed once at the
epic level (e.g., a shared middleware/pattern named in `architecture.md`) rather than per story.

## 7. Epic-level traceability
Do the FR/NFR IDs `epic-map.json` assigns to this epic still match `PRD.md`'s current wording? If
the PRD was edited after decomposition, `epic-map.json` and the stories built from it can quietly
drift out of sync — check this once for the whole epic rather than per story.

## Prioritizing the interview
Rank surviving gaps by blast radius: an actor/role used by half the epic's stories, or a silently-
assumed cross-epic dependency, outranks a single contradicting requirement claim, which outranks
a dependency-note mismatch, which outranks a wording nitpick. Ask the ones that would otherwise
get independently rediscovered (and re-asked) by every affected story's own `story-design` first.
