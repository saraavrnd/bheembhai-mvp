# Story Design — LEARN-21: Upload course materials

> Lives at `docs/product/epics/LEARN-11/stories/LEARN-21/story-design.md` (per-story folder; the
> folder carries the key, so the filename is just `story-design.md`).

**Status:** APPROVED · **Epic:** LEARN-11 · **Date:** 2026-06-19

## Target module(s)
`apps/api/app/ingestion/` — owns material upload and preparation per architecture.md.

## Interfaces / endpoints
| Kind | Signature | Request → Response |
|------|-----------|--------------------|
| HTTP | `POST /api/courses/{course_id}/materials` | multipart file → `201 {material_id, status:"processing"}` |
| HTTP | (error) same endpoint | unsupported type → `415 {error:"Unsupported file type"}` |
| HTTP | (error) same endpoint | >25 MB → `413 {error:"File exceeds 25 MB limit"}` |
| fn | `ingestion.accept_upload(course_id, file) -> Material` | validates type+size, persists, enqueues prep |

## Data-model deltas
New `material` row: `id, course_id (fk), filename, mime_type, size_bytes, status, created_at`.
Consistent with `data-model.md` (material entity already sketched there). One migration.

## Reuse
- Existing `db` session + `Course` model.
- Existing error-response helper from the walking skeleton's API conventions.
- Existing storage path config from `.env`.

## Approach
Add the route to the ingestion module. Validate MIME type against an allowlist (PDF/DOCX/TXT)
and size against the 25 MB limit before storing anything; on rejection, return the specific
error and persist nothing. On success, store the file, create the `material` row with
status="processing", and enqueue the prep job (the prep job itself is LEARN-22, out of scope
here). Status transitions beyond "processing" belong to LEARN-22.

## Test surface
- [x] Unit: validation logic (type allowlist, size limit) — pure function.
- [x] Integration: the three endpoint scenarios (accept, 415, 413) against a test DB.
- [ ] E2E: deferred to when the upload UI lands; API-level integration is sufficient for this story.

## Out of scope
Processing/chunking the file (LEARN-22); editing/versioning materials; bulk upload.

## Architecture impact
None — fits existing architecture. Single module, single new entity already anticipated in the
data model.

## Traceability
**Requirements covered:** FR-004

---
*Approved. Next: `test-creator` writes failing unit + integration tests from the three
acceptance scenarios and these signatures, then `implement`.*
