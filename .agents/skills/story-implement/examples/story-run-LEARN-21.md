# Story Run — LEARN-21: Upload course materials

**Entry point:** story-implement · **Branch:** `feat/LEARN-21-upload-course-materials` · **Date:** 2026-06-19

> Invoked as: "implement LEARN-21". Shows the loops in action — one test-verify BLOCK and one
> accepted code-review fix — not just the happy path.

## Sequence executed
| Step | Skill | Result |
|------|-------|--------|
| 1 | story-design | approved (Checkpoint 1) — no escalation; fits ingestion module |
| 2 | test-creator | 5 failing tests (red) from the 3 acceptance scenarios + validation |
| 3 | implement | green (first pass) |
| 4 | test-verify | BLOCK → then PASS (see loops) |
| 5 | code-review | 1 High + 2 Medium + 1 Nit; High accepted (see loops) |
| 6 | pr-create (+design-sync) | PR #37 opened (Checkpoint 2) |

## Loops taken
1. **Checkpoint 1:** story-design note presented; user approved as-is. No architecture escalation.
2. **test-verify BLOCK (Step 4):** first verification found the oversized-file test passed via a
   generic 400 rather than the required 413 — routed to `implement`, fixed to return 413 with the
   exact message, **re-ran test-verify → PASS**.
3. **code-review fix loop (Step 5):** advisory report flagged a High (type allowlist trusts the
   client content-type → spoofable). User accepted it; routed to `implement`, which switched to
   content-signature validation and `test-creator` added a spoofed-content-type test; **re-ran
   test-verify → PASS**. The 2 Mediums + Nit were logged but waived for this PR.

## Checkpoints
- Checkpoint 1 (story-design approval): approved
- Checkpoint 2 (PR review/merge): PR #37 open, awaiting human review

## design-sync
Incremental: added `POST /api/courses/{course_id}/materials` to architecture + contracts, promoted
`material` table in data-model.md, updated AGENTS.md (endpoint + entity). Flagged **LEARN-22**
(depends on the `material.status` field) with a comment to re-check its story-design.

## Outcome
- PR: https://github.com/<org>/learn-portal/pull/37
- Jira status: To Do → In Progress (set when story-implement started) → In Review (set by pr-create)
- Requirements covered: FR-004

---
*Loop complete pending human review. Next story: invoke `story-implement` with its key (e.g.
"implement LEARN-22" — noting its design flag from this run).*
