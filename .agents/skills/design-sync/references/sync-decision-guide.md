# Sync Decision Guide

How to keep per-merge sync proportionate: most merges should be near no-ops, real changes update
only what moved, and structural changes shouldn't be reaching this skill un-amended.

## Classify every merge into one of three

### No system-level change (most common)
The story implemented behavior the design already anticipated — a handler filled in, internal
logic added, an endpoint that was already in the contracts now works.
**Action:** append one line to `docs/CHANGELOG-design.md` and stop. Do NOT rewrite the
architecture narrative. A near no-op is correct and fast — resist the urge to "tidy" docs that
didn't change.

### Incremental change
The story added or altered something the references should now show:
- a new endpoint or a changed request/response shape,
- a new or changed data field/table,
- a new dependency between modules,
- a new config/env var.
**Action:** update only the affected sections — the endpoint list, the data-model entry, the
module description, the contract file, and the relevant AGENTS.md summary lines. Touch only what
changed.

### Structural change (should be rare here)
A new external integration, a new service, a new cross-cutting concern, or anything contradicting
an ADR. This should have come through `story-design`'s escalation → `tech-design` amendment
BEFORE implementation.
**Action:** if you see a structural change that did NOT go through a tech-design amendment, that
means the escalation rule was bypassed. Flag it loudly in the sync note and recommend pausing to
amend `tech-design` + add an ADR. Do not silently absorb a structural change into the docs as if
it were incremental.

## Idempotency & concurrent merges (multi-user safety)
- Always reconcile against the CURRENT state of `main`, not a snapshot taken when the story
  started. Two stories may merge minutes apart.
- Make updates additive and targeted so re-running the sync produces no further change (idempotent).
- If another merge already recorded the same endpoint/field, don't duplicate it — detect and skip.
This is what lets parallel merges converge instead of clobbering each other's doc edits.

## Keep AGENTS.md current but lean
AGENTS.md is read first by every future story's agent. Update its summaries (modules, endpoints,
entities, conventions) but keep it an orientation file, not a spec — push detail to
architecture.md / data-model.md / contracts and link to them. Stale AGENTS.md degrades every
downstream skill, so it's the highest-priority file to keep fresh — but bloat makes it useless,
so summarize.

## Scope discipline
Docs and reference files only. If you find yourself wanting to change code or a test to "match
the docs", stop — that's a real defect or a design issue, route it to implement / story-design,
don't paper over it in the sync.
