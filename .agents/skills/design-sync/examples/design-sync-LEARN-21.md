# Design Sync — LEARN-21: Upload course materials

**Date:** 2026-06-19 · **Branch:** `feat/LEARN-21-upload-course-materials` · **Classification:** Incremental

## What this story changed (vs current design)
- New endpoint: `POST /api/courses/{course_id}/materials` (was anticipated as a feature area but
  not yet in the current-endpoints list).
- New data entity row in use: `material` table created (it was sketched in data-model.md; now
  real with concrete fields).
- No new module dependency, no new integration, no new cross-cutting concern.

## Reference updates committed to this branch
- [x] `architecture.md` — added the endpoint to the ingestion module's "current endpoints" line.
- [x] `data-model.md` — promoted `material` from sketch to concrete: `id, course_id, filename,
      mime_type, size_bytes, status, created_at`.
- [x] `api-contracts/materials.yaml` — added the POST contract (request multipart, 201/415/413).
- [x] `AGENTS.md` — added the endpoint to "Current endpoints", `material` to "Data entities".
- [x] `docs/CHANGELOG-design.md` — entry: "LEARN-21: material upload endpoint + material table."

## Affected in-progress stories (flagged for design re-check)
| Story | Overlap (module / entity) | Action |
|-------|---------------------------|--------|
| LEARN-22 (Prepare materials for retrieval) | depends on `material` table + status field | commented on LEARN-22: "material table is now live with `status`; re-check your story-design before implementing — your prep job should transition this exact status field" |

## Structural-change check
None — incremental, fully within the existing architecture. No tech-design amendment needed.

---
*Docs committed to the branch; `pr-create` proceeds to merge them atomically with the code.
LEARN-22's owner has been notified to re-check design against the now-live `material` table.*
