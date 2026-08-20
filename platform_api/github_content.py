"""Stage-accurate file content for the run-details viewer.

The viewer's fallback chain is git → stub → placeholder:

1. ``git`` — fetch the file from the GitHub remote at the step's recorded
   commit SHA (contents API, raw media type). Only entered when the run has a
   GitHub integration, a token resolves, and the step has a content-bearing
   transition payload with a ``commit``.
2. ``stub`` / ``placeholder`` — legacy demo content. (ADR-014 removed the
   clone-tree stage: the engine's ``BB_WORKDIR`` clone no longer exists, so
   anything git cannot serve — including 404s for never-committed generated
   artifacts like ``changes.diff`` — falls straight to stubs.)

Every stage of the chain is non-raising: the viewer always answers.
"""

from __future__ import annotations

import logging

import httpx
from bheembhai.github import (
    ARTIFACT_TEXT_MAX,
    api_base_from_config,
    fetch_file_at_commit,
    repo_slug_from_config,
)
from bheembhai.models.project import ProjectIntegration
from bheembhai.models.run import Run, Transition
from bheembhai.resolver import mask_credential, resolve_credentials
from sqlalchemy import select

logger = logging.getLogger(__name__)

# Mirrors the display keys in platform_api.routers.runs._latest_step_payload —
# a transition row only "has content" if one of these is present.
_DISPLAY_PAYLOAD_KEYS = ("files", "review_files", "summary", "artifact", "commit")


def _content_commits(rows: list[Transition]) -> list[str]:
    """SHAs from content-bearing transition payloads, newest row last."""
    shas: list[str] = []
    for row in rows:
        payload = dict(row.payload or {})
        if any(k in payload for k in _DISPLAY_PAYLOAD_KEYS) and payload.get("commit"):
            shas.append(str(payload["commit"]))
    return shas


async def resolve_step_sha(db, run_id, step_id: str, commit: str | None) -> str | None:
    """Authoritative SHA for (run, step): the caller's ``commit`` wins only if it
    appears in this run's recorded payloads (multi-visit loops store a different
    SHA per visit); otherwise the newest recorded SHA; else None.

    Deliberately NOT filtered by to_state: the engine records verdict rows for
    non-happy results (BLOCK / changes_requested / escalation_required) with
    to_state="failed" (state_machine convention) — a to_state filter here
    excluded run cafbe28c's changes_requested row, resolved no SHA, and the
    viewer fell through to the placeholder. The content-key discriminator
    below is the authority."""
    stmt = (
        select(Transition)
        .where(Transition.run_id == run_id,
               Transition.step_id == step_id)
        .order_by(Transition.id.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    shas = _content_commits(list(rows))
    if commit:
        return commit if commit in shas else None
    return shas[0] if shas else None


async def git_fetch_content(db, run: Run, sha: str, path: str, secure_storage) -> str | None:
    """Fetch one file at a commit SHA from the run's GitHub integration.

    Returns the decoded text, or None on any miss — no integration, unresolvable
    token, malformed config, or a failed request (404/401/403/5xx/network/
    oversized/binary). Never raises: the chain falls through to the stubs.
    """
    if not run.github_integration_id:
        return None

    integ = await db.get(ProjectIntegration, run.github_integration_id)
    if integ is None:
        return None

    resolved = await resolve_credentials([integ], secure_storage)
    if not resolved:
        return None

    api_base = api_base_from_config(integ.config or {})
    repository = repo_slug_from_config(integ.config or {})
    if not api_base or not repository:
        logger.info("git fetch: integration %s has malformed config — skipping",
                    integ.id)
        return None

    token = resolved[0].token
    logger.info("git fetch %s@%s %s (token ...%s)", repository, sha, path,
                mask_credential(token))
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        content, _status = await fetch_file_at_commit(
            client, api_base=api_base, token=token, repository=repository,
            path=path, ref=sha, max_bytes=ARTIFACT_TEXT_MAX)
    return content


def build_chain(
    git_content: str | None,
    path: str,
    stub_content: dict | None = None,
) -> tuple[str, str, str]:
    """Pure ordering of the viewer chain. Returns ``(content, source, path)`` —
    ``path`` may be rewritten by a stub substring match (the caller derives the
    viewer type from the resolved path).

    ``stub_content`` is the module-level ``_STUB_FILE_CONTENT`` mapping (passed
    in so this stays importable without the router).
    """
    if git_content is not None:
        return git_content, "git", path
    if stub_content:
        if path in stub_content:
            return stub_content[path], "stub", path
        for key, val in stub_content.items():
            if path in key or key in path:
                return val, "stub", key
    return f"# {path}\n\nFile content not available.", "placeholder", path
