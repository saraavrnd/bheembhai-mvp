"""Unit tests — run init pure parts: branch naming, GitHub URL normalization,
and the REST branch-creation client (idempotency + failure classification)."""

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest

from engine_service.run_init import (
    GitTarget,
    InitFailure,
    compose_git_target,
    create_branch_github,
    derive_run_branch,
    safe_story,
)

# ── Branch naming ──────────────────────────────────────────────────────

def test_safe_story_folds_to_slug():
    assert safe_story("LNPRTL-101") == "lnprtl-101"
    assert safe_story("Hello World!!") == "hello-world"
    assert safe_story("  ") == "story"
    assert safe_story(None) == "story"


def test_derive_run_branch_format():
    run_id = uuid.UUID("12345678-1234-1234-1234-123456789abc")
    now = datetime(2026, 8, 14, 9, 30, tzinfo=timezone.utc)
    name = derive_run_branch("LNPRTL-101", run_id, now=now)
    assert name == "feat/lnprtl-101/140820260930-1234"


def test_derive_run_branch_suffix_comes_from_run_uuid():
    a = derive_run_branch("S-1", uuid.uuid4(),
                          now=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    b = derive_run_branch("S-1", uuid.uuid4(),
                          now=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))
    assert a != b   # uuid suffix disambiguates same-minute same-story runs


# ── GitHub URL normalization ───────────────────────────────────────────

def test_compose_defaults_to_github_dot_com():
    t = compose_git_target({"url": "", "repository": "acme/demo"})
    assert t == GitTarget(api_base="https://api.github.com",
                          clone_url="https://github.com/acme/demo.git",
                          repository="acme/demo")


def test_compose_keeps_explicit_api_host_and_maps_clone_base():
    t = compose_git_target({"url": "https://api.github.com", "repository": "acme/demo"})
    assert t.api_base == "https://api.github.com"
    assert t.clone_url == "https://github.com/acme/demo.git"   # cloneable host, not the API


def test_compose_enterprise_url_gets_api_v3():
    t = compose_git_target({"url": "https://git.corp.example", "repository": "acme/demo"})
    assert t.api_base == "https://git.corp.example/api/v3"
    assert t.clone_url == "https://git.corp.example/acme/demo.git"


def test_compose_full_url_repository_used_verbatim():
    t = compose_git_target({"url": "https://github.com",
                            "repository": "https://github.com/acme/demo"})
    assert t.clone_url == "https://github.com/acme/demo"    # verbatim — nothing appended
    assert t.repository == "acme/demo"


def test_compose_ssh_repository_used_verbatim():
    t = compose_git_target({"url": "https://github.com",
                            "repository": "git@github.com:acme/demo.git"})
    assert t.clone_url == "git@github.com:acme/demo.git"
    assert t.repository == "acme/demo"


def test_compose_missing_repository_is_failed_execution():
    with pytest.raises(InitFailure) as ei:
        compose_git_target({"url": "https://github.com", "repository": ""})
    assert ei.value.kind == "failed_execution"


def test_compose_repository_without_owner_is_failed_execution():
    with pytest.raises(InitFailure) as ei:
        compose_git_target({"url": "https://github.com", "repository": "just-aname"})
    assert ei.value.kind == "failed_execution"


# ── Branch creation via GitHub REST ────────────────────────────────────

class GitHubMock:
    """Scriptable GitHub REST — records calls, returns canned refs."""

    def __init__(self, source_branch="main", source_sha="abc123"):
        self.source_branch = source_branch
        self.source_sha = source_sha
        self.existing: dict[str, str] = {}    # branch -> sha
        self.fail: tuple[int, str] | None = None   # force every call to this response
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            status, text = self.fail
            return httpx.Response(status, text=text)
        if request.method == "GET":
            branch = request.url.path.rsplit("/heads/", 1)[-1]   # branches contain slashes
            sha = self.source_sha if branch == self.source_branch else self.existing.get(branch)
            if sha is None:
                return httpx.Response(404, text="Not Found")
            return httpx.Response(200, json={"object": {"sha": sha}})
        if request.method == "POST":
            body = json.loads(request.content)
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.existing:
                return httpx.Response(422, text="Reference already exists")
            self.existing[branch] = body["sha"]
            return httpx.Response(201, json={})
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


TARGET = GitTarget(api_base="https://api.github.com",
                   clone_url="https://github.com/acme/demo.git",
                   repository="acme/demo")


async def test_create_branch_happy_path():
    mock = GitHubMock()
    async with mock.client() as client:
        name = await create_branch_github(TARGET, "ghp_secret", "main", "feat/x", client=client)
    assert name == "feat/x"
    # GET source ref, then POST refs with the right ref + sha + bearer token
    get, post = mock.requests
    assert get.method == "GET"
    assert get.url.path == "/repos/acme/demo/git/ref/heads/main"
    assert get.headers["Authorization"] == "Bearer ghp_secret"
    assert post.method == "POST"
    assert json.loads(post.content) == {"ref": "refs/heads/feat/x", "sha": "abc123"}


async def test_create_branch_idempotent_on_same_sha():
    mock = GitHubMock()
    mock.existing["feat/x"] = "abc123"    # prior init already created it
    async with mock.client() as client:
        name = await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert name == "feat/x"
    assert [r.method for r in mock.requests] == ["GET", "POST", "GET"]  # no bump


async def test_create_branch_suffix_bump_on_different_sha():
    mock = GitHubMock()
    mock.existing["feat/x"] = "def456"    # same name, wrong sha — a stale branch
    async with mock.client() as client:
        name = await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert name == "feat/x-2"
    post2 = mock.requests[-1]
    assert json.loads(post2.content)["ref"] == "refs/heads/feat/x-2"


async def test_create_branch_double_collision_fails_execution():
    mock = GitHubMock()
    mock.existing = {"feat/x": "def456", "feat/x-2": "def456"}
    async with mock.client() as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert ei.value.kind == "failed_execution"
    assert "cleanup" in ei.value.reason


async def test_create_branch_auth_401_is_failed_execution():
    mock = GitHubMock()
    mock.fail = (401, '{"message": "Bad credentials"}')
    async with mock.client() as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "bad", "main", "feat/x", client=client)
    assert ei.value.kind == "failed_execution"


async def test_create_branch_server_5xx_is_failed_infra():
    mock = GitHubMock()
    mock.fail = (502, "Bad gateway")
    async with mock.client() as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert ei.value.kind == "failed_infra"


async def test_create_branch_unknown_source_branch_404_is_failed_execution():
    mock = GitHubMock(source_branch="main")
    async with mock.client() as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "t", "develop", "feat/x", client=client)
    assert ei.value.kind == "failed_execution"
    assert "404" in ei.value.reason


async def test_create_branch_network_error_is_failed_infra():
    def down(request):  # connection refused — httpx surfaces it as ConnectError
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert ei.value.kind == "failed_infra"


async def test_create_branch_validation_422_without_existing_ref():
    """A 422 that isn't 'ref exists' (e.g. protected branch) must surface the
    POST body, not a confusing 404 from the follow-up GET."""
    mock = GitHubMock()
    mock.fail = None

    def refuse(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(422, text='{"message": "protected branch"}')
        return mock.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        with pytest.raises(InitFailure) as ei:
            await create_branch_github(TARGET, "t", "main", "feat/x", client=client)
    assert ei.value.kind == "failed_execution"
    assert "protected branch" in ei.value.reason
