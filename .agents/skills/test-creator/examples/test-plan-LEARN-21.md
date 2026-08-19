# Test Plan — LEARN-21: Upload course materials

**Epic:** LEARN-11 · **Date:** 2026-06-19 · **Runners:** pytest (unit + integration)

## Scenario → test mapping
| # | Acceptance scenario | Layer | Test file::test name | Type |
|---|---------------------|-------|----------------------|------|
| 1 | Accepted upload (≤25 MB, PDF/DOCX/TXT) → 201, status "processing" within 2s | integration | `tests/integration/ingestion/test_materials_upload.py::test_accepts_valid_upload` | happy |
| 2 | Unsupported format → 415, message "Unsupported file type", nothing stored | integration | `tests/integration/ingestion/test_materials_upload.py::test_rejects_unsupported_type` | unhappy |
| 3 | Oversized file (>25 MB) → 413, message "File exceeds 25 MB limit", nothing stored | integration | `tests/integration/ingestion/test_materials_upload.py::test_rejects_oversized_file` | unhappy |
| 4 | Validation logic: type allowlist | unit | `tests/unit/ingestion/test_upload_validation.py::test_type_allowlist` | unhappy |
| 5 | Validation logic: size limit boundary (25 MB exactly vs over) | unit | `tests/unit/ingestion/test_upload_validation.py::test_size_boundary` | unhappy |

## Coverage check
- [x] Every acceptance scenario has at least one test (scenarios 1-3 → tests 1-3; validation
      detail → tests 4-5).
- [x] Unhappy paths covered (type, size, boundary).
- [x] Tests target the interfaces from the approved story-design note
      (`POST /api/courses/{course_id}/materials`, `ingestion.accept_upload`).

## Red-phase verification
```
$ pytest tests/unit/ingestion/test_upload_validation.py tests/integration/ingestion/test_materials_upload.py
...
tests/unit/ingestion/test_upload_validation.py::test_type_allowlist FAILED
    > from app.ingestion.validation import is_allowed_type
    E   ImportError -> FIXED: created empty validation.py stub so tests FAIL on assertion, not import
tests/unit/ingestion/test_upload_validation.py::test_type_allowlist FAILED  (AssertionError: function returns None)
tests/integration/ingestion/test_materials_upload.py::test_accepts_valid_upload FAILED  (404: route not registered)
tests/integration/ingestion/test_materials_upload.py::test_rejects_unsupported_type FAILED  (404)
tests/integration/ingestion/test_materials_upload.py::test_rejects_oversized_file FAILED  (404)
tests/unit/ingestion/test_upload_validation.py::test_size_boundary FAILED  (AssertionError)

5 failed — all GOOD red (feature missing: route 404, validation unimplemented).
```
Note: the first run errored on import (bad red); added an empty `validation.py` + unregistered
route stub so the tests now fail cleanly on assertions/404 (good red). No feature logic added.

## Traceability
**Requirements covered:** FR-004

---
*Next: `implement` makes these 5 tests pass without weakening them.*
