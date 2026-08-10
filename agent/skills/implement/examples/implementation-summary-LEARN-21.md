# Implementation Summary — LEARN-21: Upload course materials

**Branch:** `feat/LEARN-21-upload-course-materials` · **Epic:** LEARN-11 · **Date:** 2026-06-19

## What changed
- `app/ingestion/validation.py` — `is_allowed_type()` (PDF/DOCX/TXT allowlist) and
  `is_within_size_limit()` (25 MB).
- `app/ingestion/routes.py` — `POST /api/courses/{course_id}/materials`: validates type then
  size, stores file + creates `material` row (status="processing") on success, returns the
  specific 415/413 errors with no side effects on rejection.
- `app/ingestion/service.py` — `accept_upload()` per the design note signature.
- `migrations/0003_material.py` — new `material` table.

## Tests passing
```
$ pytest && ruff check .
tests/unit/ingestion/test_upload_validation.py::test_type_allowlist PASSED
tests/unit/ingestion/test_upload_validation.py::test_size_boundary PASSED
tests/integration/ingestion/test_materials_upload.py::test_accepts_valid_upload PASSED
tests/integration/ingestion/test_materials_upload.py::test_rejects_unsupported_type PASSED
tests/integration/ingestion/test_materials_upload.py::test_rejects_oversized_file PASSED
... (full suite) 14 passed
ruff: clean
```

## Acceptance criteria satisfied
- [x] Scenario 1 — valid upload → 201 + status "processing".
- [x] Scenario 2 — unsupported type → 415 "Unsupported file type", nothing stored.
- [x] Scenario 3 — oversized → 413 "File exceeds 25 MB limit", nothing stored.

## Design adherence
- [x] Code in `app/ingestion/` per the story-design note.
- [x] Built to the approved endpoint + `accept_upload` signature.
- [x] No test weakened/skipped to go green (first run was 5 failing; now passing on real logic).
- [x] No unplanned architecture change.

## Data-model changes
`0003_material` migration — `material` table as specified.

## Traceability
**Requirements covered:** FR-004

---
*Next: `test-verify` (independent gate), then `pr-create`.*
