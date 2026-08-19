# Dependency Detection — three sources

How `epic-sequence` infers which story must come before which. Combine all three; when they
disagree, trust the more reliable source and flag the conflict.

## Source 1 — Explicit Jira links (ground truth)
"blocks" / "is blocked by" links a human set on the stories. Authoritative — never override or
remove them. If your artifact/requirement inference contradicts an explicit link, surface the
contradiction to the user; do not silently change it. Humans know things the artifacts don't.

## Source 2 — Artifact-derived (richest signal)
From each story's `story-design` note, build a produces/consumes profile:

**Produces** (this story creates it):
- new endpoints (e.g. `POST /materials`),
- new tables/fields (e.g. creates `material` table, adds `material.checksum`),
- new modules / shared functions.

**Consumes** (this story needs it to exist):
- endpoints it calls,
- tables/fields it reads or writes,
- modules/functions it builds on.

**Edge rule:** story B depends on story A (A → B, "A before B") when B consumes something A
produces. Example: B's note says it reads `material.status`; A's note says it creates `material`
with a `status` field → A before B.

Stories without a design note yet have no produces/consumes profile — order them via Source 3
until they're designed, then re-run to sharpen.

## Source 3 — Requirement / data-model derived (coarse, always available)
From `epic-map`/`story-map` requirement IDs + `data-model.md`:
- A story that creates a foundational entity comes before stories that use that entity.
- A requirement that is a prerequisite of another (e.g. "upload a document" before "process the
  document") implies story order.
This is coarser than artifact-derived but always available, so it covers un-designed stories and
acts as a sanity check on the finer signals.

## Recording edge provenance
For every edge, record which source produced it: `link` / `artifact` / `requirement`. This makes
the proposed order auditable — the user can trust the explicit links, scrutinise the inferred ones,
and correct a wrong inference before any Jira link is written.

## Cycles
If edges form a cycle (A → B → A, directly or transitively), do NOT pick an arbitrary order to
mask it. A cycle almost always means:
- two stories are really two halves of one capability (re-merge or re-split in `user-story`), or
- a shared piece should be extracted into its own foundational story that both depend on.
Flag the cycle with the stories and the conflicting edges; let the user resolve it. An honest
"these are circular, fix the slicing" is more useful than a fake order.

## Waves & parallelism
After a clean topological sort, group into waves:
- Wave 1: no dependencies.
- Wave N: depends only on waves < N.
Stories in the same wave are independent → safe to build in parallel (assign to different
developers). This is how the sequence supports parallel work without collisions; `design-sync`
then keeps shared references current as each wave merges.
