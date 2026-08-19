# Epic Sequence — LEARN-11: Curriculum knowledge base

**Status:** APPROVED (provisional — refined as designs land) · **Date:** 2026-06-19 · **Stories:** 3

> First run was **provisional** (no story-design notes existed yet): the order came from
> requirement traceability (FR-004 → FR-005 → FR-006) + acceptance-criteria text ("the uploaded
> document", "prepared chunks"). As LEARN-21 and LEARN-22 were designed inside `story-implement`,
> their dependency-deltas confirmed the edges — they're now **firm**. The order didn't change.

## Build order (waves)

### Wave 1 (no dependencies — start here)
- LEARN-21 — Upload course materials

### Wave 2 (depends only on Wave 1)
- LEARN-22 — Prepare materials for retrieval  (depends on: LEARN-21)

### Wave 3 (depends on Wave 2)
- LEARN-23 — Ground tutoring responses in retrieved material  (depends on: LEARN-22)

## Dependencies (with source + confidence)
| Story | Depends on | Source | Confidence | Why |
|-------|-----------|--------|-----------|-----|
| LEARN-22 | LEARN-21 | requirement → artifact | provisional → **firm** | FR-005 presupposes FR-004; LEARN-22's design note confirmed it reads the `material` row + `status` LEARN-21 creates |
| LEARN-23 | LEARN-22 | acceptance-text → artifact | provisional → **firm** | "retrieved/prepared material" hinted it; LEARN-22/23 designs confirmed chunks exist only after LEARN-22 processes a material |

> The "→" shows the refinement: each edge started provisional (first run, no designs) and became
> firm once the relevant story-design emitted its dependency-delta. Had a design revealed a *new*
> or *contradicting* dependency, `epic-sequence` would have re-sequenced the remaining stories and
> adjusted the Jira links.

## Parallelizable groups
None in this epic — it's a linear pipeline (upload → prepare → ground). Each wave has one story,
so they run in sequence. (In epics with independent features, a wave would hold several stories
that different developers could take at once.)

## Cycles / conflicts flagged (need a human decision)
None — clean linear chain. No inference contradicted a human-set link (there were none set).

## On approval
- Set "blocks" links via Atlassian MCP: LEARN-21 blocks LEARN-22; LEARN-22 blocks LEARN-23.
- Wrote `docs/product/epics/LEARN-11/_epic/epic-sequence.json`.

---
*`story-implement` will pick LEARN-21 first (wave 1, no deps), then LEARN-22 once LEARN-21 is
merged, then LEARN-23. Re-run epic-sequence if stories are added or re-designed.*
