# PR-Create — LEARN-21: Upload course materials

**Pre-flight:** verification-LEARN-21.md = PASS ✓ · **Branch:** `feat/LEARN-21-upload-course-materials`

## Assembled PR body (posted via GitHub MCP)

> **Title:** LEARN-21: Upload course materials
>
> **What this PR does**
> Adds `POST /api/courses/{course_id}/materials` in the ingestion module: validates file type
> (PDF/DOCX/TXT) and size (≤25 MB), stores valid files and creates a `material` row with
> status "processing", and returns specific errors with no side effects on rejection.
>
> **Acceptance criteria covered**
> - [x] Valid upload → 201, status "processing"
> - [x] Unsupported type → 415 "Unsupported file type", nothing stored
> - [x] Oversized → 413 "File exceeds 25 MB limit", nothing stored
>
> **Tests**
> - 5 new tests (2 unit, 3 integration); full suite 14 passed; coverage 94% on ingestion; lint clean
> - Verified independently by test-verify (PASS)
>
> **Traceability:** FR-004
> **Jira:** LEARN-21

## GitHub MCP result
- Branch pushed.
- PR opened: `https://github.com/<org>/learn-portal/pull/37` (captured).

## Atlassian MCP result
- PR link added to LEARN-21 as a remote link.
- LEARN-21 transitioned: "In Progress" → **"In Review"** (confirmed transition name in the
  LEARN workflow).

## Hand-off
PR #37 is open and ready for human review; LEARN-21 is In Review. Not merged — review owns
merge. The next story can begin at `story-design`.
