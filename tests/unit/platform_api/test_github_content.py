"""Unit tests — step-sha resolution and the viewer fallback chain.

Fakes stand in for the DB (SimpleNamespace rows/execute) and for the network
(monkeypatched resolver + fetch), so the orchestration logic is exercised
without Postgres or a GitHub token.
"""

from types import SimpleNamespace

from platform_api.github_content import (
    build_chain,
    git_fetch_content,
    resolve_step_sha,
)

# ── resolve_step_sha ────────────────────────────────────────────────────

def _fake_db(rows_newest_first):
    async def execute(stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows_newest_first))
    return SimpleNamespace(execute=execute)


async def test_resolve_step_sha_recorded_commit_wins():
    db = _fake_db([
        SimpleNamespace(payload={"commit": "def4567", "files": []}),
        SimpleNamespace(payload={"commit": "abc1234", "summary": "x"}),
    ])
    assert await resolve_step_sha(db, "run-1", "implement", "abc1234") == "abc1234"


async def test_resolve_step_sha_bogus_commit_rejected():
    db = _fake_db([
        SimpleNamespace(payload={"commit": "def4567", "files": []}),
    ])
    assert await resolve_step_sha(db, "run-1", "implement", "zzz9999") is None


async def test_resolve_step_sha_without_commit_returns_newest():
    db = _fake_db([
        SimpleNamespace(payload={"commit": "def4567", "summary": "later visit"}),
        SimpleNamespace(payload={"commit": "abc1234", "files": []}),
    ])
    assert await resolve_step_sha(db, "run-1", "implement", None) == "def4567"


async def test_resolve_step_sha_skips_contentless_rows():
    # The empty approval record must not hide the completion payload, and a
    # row whose payload lacks `commit` (or has none) is not a usable sha.
    db = _fake_db([
        SimpleNamespace(payload={}),
        SimpleNamespace(payload={"files": [{"path": "a.py"}]}),
        SimpleNamespace(payload={"commit": None, "summary": "x"}),
        SimpleNamespace(payload={"commit": "def4567", "files": []}),
    ])
    assert await resolve_step_sha(db, "run-1", "implement", None) == "def4567"


async def test_resolve_step_sha_no_content_bearing_rows():
    db = _fake_db([SimpleNamespace(payload={})])
    assert await resolve_step_sha(db, "run-1", "implement", None) is None


async def test_resolve_step_sha_non_happy_verdict_row_is_usable():
    # The engine records changes_requested / BLOCK / escalation rows with
    # to_state="failed" — their commit is a legitimate pin (run cafbe28c's
    # code-review visit pushed 105c655 and the viewer must be able to read it).
    db = _fake_db([
        SimpleNamespace(payload={"commit": "105c655", "summary": "review done",
                                 "result_status": "changes_requested"}),
    ])
    assert await resolve_step_sha(db, "run-1", "code-review", None) == "105c655"
    assert await resolve_step_sha(db, "run-1", "code-review", "105c655") == "105c655"


async def test_resolve_step_sha_query_has_no_to_state_filter():
    # Regression (run cafbe28c): a to_state filter in the query excluded the
    # changes_requested verdict row (to_state="failed") → no SHA → the viewer
    # served the "File content not available" placeholder.
    captured = {}

    async def execute(stmt):
        captured["stmt"] = stmt
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

    db = SimpleNamespace(execute=execute)
    assert await resolve_step_sha(db, "run-1", "code-review", None) is None
    where_clause = str(captured["stmt"].compile()).split("WHERE")[-1]
    assert "to_state" not in where_clause


# ── git_fetch_content ───────────────────────────────────────────────────

async def test_git_fetch_no_integration_id_returns_none():
    run = SimpleNamespace(github_integration_id=None)
    assert await git_fetch_content(None, run, "abc1234", "a.py", None) is None


async def test_git_fetch_integration_row_missing_returns_none():
    db = SimpleNamespace()
    async def get(model, pk):
        return None
    db.get = get
    run = SimpleNamespace(github_integration_id="int-1")
    assert await git_fetch_content(db, run, "abc1234", "a.py", None) is None


async def test_git_fetch_unresolvable_token_returns_none(monkeypatch):
    async def no_creds(integrations, secure_storage):
        return []
    monkeypatch.setattr("platform_api.github_content.resolve_credentials", no_creds)

    integ = SimpleNamespace(id="int-1", config={"repository": "o/r"})
    db = SimpleNamespace()
    async def get(model, pk):
        return integ
    db.get = get
    run = SimpleNamespace(github_integration_id="int-1")
    assert await git_fetch_content(db, run, "abc1234", "a.py", None) is None


async def test_git_fetch_malformed_config_returns_none(monkeypatch):
    async def one_cred(integrations, secure_storage):
        return [SimpleNamespace(token="tok")]
    monkeypatch.setattr("platform_api.github_content.resolve_credentials", one_cred)

    integ = SimpleNamespace(id="int-1", config={"repository": ""})
    db = SimpleNamespace()
    async def get(model, pk):
        return integ
    db.get = get
    run = SimpleNamespace(github_integration_id="int-1")
    assert await git_fetch_content(db, run, "abc1234", "a.py", None) is None


async def test_git_fetch_happy_path(monkeypatch):
    async def one_cred(integrations, secure_storage):
        return [SimpleNamespace(token="tok")]
    async def fake_fetch(client, **kw):
        return ("stage content", 200)
    monkeypatch.setattr("platform_api.github_content.resolve_credentials", one_cred)
    monkeypatch.setattr("platform_api.github_content.fetch_file_at_commit", fake_fetch)

    integ = SimpleNamespace(id="int-1", config={"repository": "o/r"})
    db = SimpleNamespace()
    async def get(model, pk):
        return integ
    db.get = get
    run = SimpleNamespace(github_integration_id="int-1")
    assert await git_fetch_content(db, run, "abc1234", "a.py", None) == "stage content"


async def test_git_fetch_failed_request_falls_through(monkeypatch):
    async def one_cred(integrations, secure_storage):
        return [SimpleNamespace(token="tok")]
    async def fake_fetch(client, **kw):
        return (None, 404)
    monkeypatch.setattr("platform_api.github_content.resolve_credentials", one_cred)
    monkeypatch.setattr("platform_api.github_content.fetch_file_at_commit", fake_fetch)

    integ = SimpleNamespace(id="int-1", config={"repository": "o/r"})
    db = SimpleNamespace()
    async def get(model, pk):
        return integ
    db.get = get
    run = SimpleNamespace(github_integration_id="int-1")
    assert await git_fetch_content(db, run, "abc1234", "a.py", None) is None


# ── build_chain ─────────────────────────────────────────────────────────

STUBS = {"changes.diff": "stub diff content", "reviewed.diff": "stub reviewed"}


def test_chain_git_wins():
    content, source, path = build_chain("git content", "a.py", STUBS)
    assert (content, source, path) == ("git content", "git", "a.py")


def test_chain_git_miss_never_returns_clone():
    # ADR-014 removed the clone-tree stage: a git miss must fall straight to
    # stubs/placeholder — "clone" is not a valid source any more.
    for path in ("a.py", "changes.diff", "out/changes.diff.txt", "unknown.md"):
        _, source, _ = build_chain(None, path, STUBS)
        assert source != "clone"


def test_chain_stub_exact_match():
    content, source, path = build_chain(None, "changes.diff", STUBS)
    assert (content, source, path) == ("stub diff content", "stub", "changes.diff")


def test_chain_stub_substring_rewrites_path():
    # Legacy clients may pass a path that only loosely matches a stub key —
    # the chain resolves it and hands back the key so the viewer type is
    # derived from the real file name.
    content, source, path = build_chain(None, "out/changes.diff.txt", STUBS)
    assert (content, source, path) == ("stub diff content", "stub", "changes.diff")


def test_chain_placeholder_terminal():
    content, source, path = build_chain(None, "unknown.md", STUBS)
    assert source == "placeholder"
    assert path == "unknown.md"
    assert "File content not available" in content
