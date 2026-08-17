"""Unit tests — shared GitHub coordinate normalization + file fetch at a commit."""

import httpx
import pytest

from bheembhai.github import (
    ARTIFACT_TEXT_MAX,
    api_base_from_config,
    fetch_file_at_commit,
    repo_slug_from_config,
)


def _client(handler, *, follow_redirects: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=follow_redirects)


FETCH_KW = dict(api_base="https://api.github.com", token="tok", repository="o/r",
                path="src/app.py", ref="abc1234")


async def test_fetch_200_returns_content_and_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="print('hi')\n")

    async with _client(handler) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content == "print('hi')\n"
    assert status == 200


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_fetch_non_200_returns_no_content_with_status(status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    async with _client(handler) as client:
        content, got = await fetch_file_at_commit(client, **FETCH_KW)
    assert content is None
    assert got == status


@pytest.mark.parametrize("exc", [httpx.TimeoutException("slow"),
                                 httpx.ConnectError("down")])
async def test_fetch_network_error_returns_none_none(exc):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    async with _client(handler) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content is None
    assert status is None


async def test_fetch_url_carries_ref_and_encoded_path():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = request.url
        seen["auth"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        await fetch_file_at_commit(client, **{**FETCH_KW, "path": "docs/my file.md"})
    assert seen["url"].params["ref"] == "abc1234"
    # URL.path decodes %-escapes — assert the raw path (minus query) so the
    # encoding is proven.
    raw_path = seen["url"].raw_path.decode().split("?", 1)[0]
    assert raw_path.endswith("/repos/o/r/contents/docs/my%20file.md")
    assert seen["auth"] == "Bearer tok"
    assert seen["accept"] == "application/vnd.github.raw+json"


async def test_fetch_refuses_oversized_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (ARTIFACT_TEXT_MAX + 1))

    async with _client(handler) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content is None
    assert status == 200


async def test_fetch_refuses_binary_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abc\x00def")

    async with _client(handler) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content is None
    assert status == 200


async def test_fetch_follows_redirects_when_enabled():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "/repos/o/r/blob-content"})
        return httpx.Response(200, text="redirected content")

    async with _client(handler) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content == "redirected content"

    # Without follow_redirects the 302 is just a non-200 — caller falls through.
    calls.clear()
    async with _client(handler, follow_redirects=False) as client:
        content, status = await fetch_file_at_commit(client, **FETCH_KW)
    assert content is None
    assert status == 302


# ── Coordinate normalization ────────────────────────────────────────────

def test_api_base_from_config_pinned_rules():
    assert api_base_from_config({}) == "https://api.github.com"
    assert api_base_from_config({"url": "https://github.com"}) == "https://api.github.com"
    assert api_base_from_config({"url": "https://api.github.com"}) == "https://api.github.com"
    assert api_base_from_config({"url": "https://git.example.com"}) == "https://git.example.com/api/v3"


def test_repo_slug_from_config_forms():
    assert repo_slug_from_config({"repository": "o/r"}) == "o/r"
    assert repo_slug_from_config({"repository": "https://github.com/o/r.git"}) == "o/r"
    assert repo_slug_from_config({"repository": "git@github.com:o/r.git"}) == "o/r"
    assert repo_slug_from_config({"repository": "ssh://git@github.com/o/r.git"}) == "o/r"


def test_repo_slug_from_config_malformed():
    assert repo_slug_from_config({}) == ""
    assert repo_slug_from_config({"repository": "   "}) == ""
    assert repo_slug_from_config({"repository": "https://o"}) == ""
    assert repo_slug_from_config({"repository": "no-slash"}) == ""
