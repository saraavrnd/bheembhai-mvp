"""GitHub coordinates + content fetch shared by both services.

The engine derives branch/clone coordinates from an integration config here
(ADR-013 §2); the platform reuses the same normalization and adds the
stage-accurate file fetch (contents API at a recorded commit SHA) for the
run-details viewer. This module must stay service-neutral: no imports from
``engine_service`` or ``platform_api``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DEFAULT_GITHUB_URL = "https://github.com"
DEFAULT_GITHUB_API = "https://api.github.com"

# GitHub's refs API returns 422 (not a 409) when the ref already exists.
GITHUB_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Content viewer cap — mirrors platform_api.routers.runs._ARTIFACT_TEXT_MAX.
ARTIFACT_TEXT_MAX = 2 * 1024 * 1024


@dataclass(frozen=True)
class GitTarget:
    """GitHub coordinates derived from an integration config."""
    api_base: str      # REST base, e.g. https://api.github.com
    clone_url: str     # what the agent container clones, e.g. https://github.com/o/r.git
    repository: str    # owner/repo slug for REST paths


def _split_base(url: str) -> tuple[str, str]:
    """(scheme, host) of a URL, tolerating missing scheme."""
    rest = url
    scheme = "https"
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    host = rest.split("/", 1)[0]
    return scheme, host


def _api_base_from_host(base: str) -> str:
    """Pinned API-base rule: github.com → api.github.com; an explicit api host is
    kept verbatim; any other host is GitHub Enterprise → {host}/api/v3."""
    scheme, host = _split_base(base)
    if host in ("github.com", "www.github.com"):
        return DEFAULT_GITHUB_API
    if host.startswith("api."):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}/api/v3"


def _clone_base(config: dict) -> str:
    """Browser/clone base for the repository URL. Someone pasting the API URL as
    the integration `url` still gets a cloneable https://github.com base."""
    base = str(config.get("url") or DEFAULT_GITHUB_URL).strip().rstrip("/")
    _, host = _split_base(base)
    if host == "api.github.com":
        base = DEFAULT_GITHUB_URL
    return base


def _slug_from_url(url: str) -> str:
    """owner/repo from a full clone URL (https, ssh, or scp-style git@ form)."""
    rest = url.rstrip("/")
    if "://" in rest:
        rest = rest.split("://", 1)[1]
    if rest.startswith("git@") and ":" in rest:
        rest = rest.split(":", 1)[1]
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        return ""
    return "/".join(parts[-2:]).removesuffix(".git")


def api_base_from_config(config: dict) -> str:
    """REST base from an integration config; ``""`` on malformed input.

    Lenient counterpart of ``compose_git_target`` (which raises) — the viewer
    treats any miss as "no git source" and falls through its chain.
    """
    base = str(config.get("url") or DEFAULT_GITHUB_URL).strip().rstrip("/")
    if not _split_base(base)[1]:
        return ""
    return _api_base_from_host(base)


def repo_slug_from_config(config: dict) -> str:
    """owner/repo from an integration config; ``""`` on malformed input."""
    repo = str(config.get("repository") or "").strip()
    if not repo:
        return ""
    if repo.startswith(("http://", "https://", "ssh://", "git@")):
        slug = _slug_from_url(repo)
        return slug if "/" in slug else ""
    return repo if "/" in repo else ""


async def fetch_file_at_commit(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    token: str,
    repository: str,
    path: str,
    ref: str,
    max_bytes: int = ARTIFACT_TEXT_MAX,
) -> tuple[str | None, int | None]:
    """Fetch one repo file at a commit/tag/branch ref via the contents API.

    Returns ``(content, status)`` — content is ``None`` whenever the caller
    should fall through (404 / auth / 5xx / network / oversized / binary).
    Never raises: this is a viewer fallback, not a control path.
    """
    try:
        resp = await client.get(
            f"{api_base}/repos/{repository}/contents/{quote(path, safe='/')}",
            params={"ref": ref},
            headers={
                **GITHUB_API_HEADERS,
                "Accept": "application/vnd.github.raw+json",
                "Authorization": f"Bearer {token}",
            },
        )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("git fetch %s@%s %s: %s", repository, ref, path, type(exc).__name__)
        return None, None

    status = resp.status_code
    if status != 200:
        # 404 = artifact never committed; 401/403 = token problem; 5xx = remote
        # hiccup. None of these are terminal for the viewer — fall through.
        logger.info("git fetch %s@%s %s: HTTP %s", repository, ref, path, status)
        return None, status

    data = resp.content
    if len(data) > max_bytes:
        logger.info("git fetch %s@%s %s: %s bytes > %s cap",
                    repository, ref, path, len(data), max_bytes)
        return None, status
    if b"\x00" in data:
        logger.info("git fetch %s@%s %s: binary refused", repository, ref, path)
        return None, status
    return data.decode("utf-8", errors="replace"), status
