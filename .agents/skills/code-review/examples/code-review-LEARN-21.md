# Code Review — LEARN-21: Upload course materials

**Advisory** · **Branch:** `feat/LEARN-21-upload-course-materials` · **Reviewed vs:** main · **Date:** 2026-06-19
**Pre-flight:** verification-LEARN-21.md = PASS

## Severity summary
| Critical | High | Medium | Low | Nit |
|:--------:|:----:|:------:|:---:|:---:|
| 0 | 1 | 2 | 0 | 1 |
**Advisory note:** Recommend fixing the 1 High (content-type trust) before PR; the 2 Mediums are
worth doing now while the code is fresh; the Nit is optional.

## Tools run
```
$ ruff check app/ingestion          -> clean
$ pip-audit                         -> no known vulns in new deps
$ gitleaks detect --staged          -> no secrets found
```

## Findings

### 1. Coding standards & conventions
| # | Severity | Location | Finding | Proposed fix |
|---|----------|----------|---------|--------------|
| S1 | Nit | routes.py:34 | Magic number `26214400` for 25 MB inline | Extract `MAX_UPLOAD_BYTES = 25 * 1024 * 1024` constant, reuse in validation |

### 2. Security audit
| # | Severity | Location | Finding (risk) | Proposed fix |
|---|----------|----------|----------------|--------------|
| S2 | High | validation.py:18 | Type allowlist checks the client-supplied `content_type` only; a malicious client can send `application/pdf` for any bytes (type spoofing) | Validate by inspecting file signature/magic bytes (or extension + sniffing), not just the request's declared content-type |
| S3 | Medium | routes.py:41 | Uploaded filename used directly when storing; could contain path separators | Sanitise/normalise the filename or store under a generated id; never trust the raw name for the path |

### 3. Acceptance-criteria intent
| # | Severity | Location | Finding | Proposed fix |
|---|----------|----------|---------|--------------|
| S4 | Medium | routes.py:50 | On the happy path the file is written before the `material` row is committed; a failure between them could leave an orphan file (criteria say "nothing stored" only for rejections, but partial success is messy) | Write file and DB row in a single transaction/cleanup-on-failure so a mid-write error leaves no orphan |

### 4. Maintainability
(no findings — module is small and cohesive; reuses existing helpers as designed)

## Fix list for `implement` (ordered by severity)
1. [High] S2 — validate file type by content signature, not the client-declared content-type.
2. [Medium] S3 — sanitise/replace the stored filename; don't trust raw input for the path.
3. [Medium] S4 — make file-write + row-create atomic with cleanup on failure.
4. [Nit] S1 — extract the 25 MB limit into a named constant.

---
*Advisory — blocks nothing. If the team accepts S2-S4: `implement` applies them → `test-verify`
re-runs (note S2 may need a new test for spoofed content-type) → `pr-create`. If all waived →
straight to `pr-create`.*
