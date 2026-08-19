# Story Review — LEARN-21: Upload course materials

**Status:** APPROVED · **Epic:** LEARN-11 · **Date:** 2026-06-18

> Worked example, run the day after `user-story` produced LEARN-21 (see
> `user-story/examples/stories-LEARN-11.md`) and the day before `story-design` picked it up (see
> `story-design/examples/story-design-LEARN-21.md`). Illustrates catching gaps at the cheapest
> point — before an interface gets designed on top of them.

## Context loaded
- `architecture.md` — ingestion module ownership.
- `data-model.md` — `Course` entity: each course has exactly one owning `teacher_id`; no
  existing `material` filename-uniqueness constraint noted.
- Sibling stories in `stories.md` (LEARN-22, LEARN-23) — no overlap with upload itself.

## Gaps found

### Gap 1 — Actor & precondition clarity
- **What was missing/ambiguous:** "Given a signed-in teacher on a subject page" doesn't say the
  teacher must be the *owner* of that course. `data-model.md` models a single owning
  `teacher_id` per course, so any signed-in teacher could otherwise upload into a course they
  don't teach.
- **Question asked:** "Should upload be restricted to the course's owning teacher, or can any
  teacher in the system upload to any course?"
- **Answer:** Restricted to the owning teacher; others should get a permission error.
- **Resulting change:** Added a precondition ("the teacher is the course's assigned owner") to
  the happy-path Given, and a new unhappy-path scenario for a non-owning teacher.

### Gap 2 — Acceptance-criteria completeness
- **What was missing/ambiguous:** No scenario for uploading a file with the same filename as an
  already-uploaded material in the same course. Silent re-upload, silent overwrite, and silent
  reject are all plausible and would each produce a different test/implementation.
- **Question asked:** "If a teacher uploads a file with a name that already exists in this
  course, should it be stored as a new, separate material, rejected, or replace the existing
  one?"
- **Answer:** Stored as a new, separate material (materials aren't deduplicated by filename;
  `material_id` is the identity).
- **Resulting change:** Added a scenario confirming duplicate filenames are both accepted and
  independently addressable by `material_id`.

### Gap 3 — Concreteness of outcomes (considered, dismissed)
- Checked "processing" status wording against LEARN-22's scenarios — already consistent, no
  change needed.

## Architecture impact
None — both gaps were resolved by pinning down intent already representable in the existing
data model (ownership check, no dedup constraint needed). Nothing here requires a new entity,
field, or integration.

## Story changes summary
`stories.md`, LEARN-21 section:
- Happy-path Given now reads: "Given a signed-in teacher who is the assigned owner of the
  course, on that course's subject page".
- New scenario: **Non-owning teacher rejected** — Given a signed-in teacher who does not own the
  course, When they attempt to upload, Then the upload is rejected with a 403 / "You do not have
  access to upload to this course" and nothing is stored.
- New scenario: **Duplicate filename accepted** — Given a course already has a material named
  `syllabus.pdf`, When the teacher uploads another file also named `syllabus.pdf`, Then it is
  accepted as a new, independent material with its own `material_id`.

## Traceability check
FR-004 wording in `PRD.md` unchanged since `user-story` ran; both new scenarios still fall under
FR-004 (no new requirement needed).

---
*On approval: `stories.md` updated in place for LEARN-21; no Jira key was given in this pass (the
pasted-text path), so no Jira update was made — the same edits should be applied to the Jira
issue once available. Next: `story-design`.*
