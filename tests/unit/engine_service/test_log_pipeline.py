"""Unit — container-log capture + object-store upload (ADR-011 engine side).

The capture must happen while the container still exists (docker logs die
with it), so reconcile's kill paths (cancel/timeout) dump first and every
other terminal return is covered by the state machine's own dump. The
upload is best-effort: a failed put must never fail the step.
"""

import asyncio
from types import SimpleNamespace

import pytest

import engine_service.runtime as rt
from engine_service.log_upload import upload_step_logs

from conftest import FakeRuntime


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(rt, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rt, "GRACE_SECONDS", 0.05)


class _LogRuntime(FakeRuntime):
    """FakeRuntime with scriptable logs()."""

    def __init__(self, logs_text="container output here", **kw):
        super().__init__(**kw)
        self.logs_text = logs_text
        self.log_calls: list[tuple] = []

    async def logs(self, h, tail=40):
        self.log_calls.append((h, tail))
        return self.logs_text


# ── _dump_container_log ──────────────────────────────────────────────


async def test_dump_container_log_writes_capture():
    r = _LogRuntime(logs_text="line a\nline b\n")
    h = await r.launch("r1", "design", 1, {})
    await rt._dump_container_log(r, h)
    target = h.result_path.parent / "container.log"
    assert target.read_text() == "line a\nline b\n"
    assert r.log_calls == [(h, rt.CONTAINER_LOG_TAIL_LINES)]


async def test_dump_container_log_idempotent_when_file_exists():
    """A second dump must not clobber the first capture (reconcile already
    wrote it on its own kill path before the state machine's blanket dump)."""
    r = _LogRuntime(logs_text="first capture")
    h = await r.launch("r1", "design", 1, {})
    await rt._dump_container_log(r, h)
    r.logs_text = "second capture"
    await rt._dump_container_log(r, h)
    assert (h.result_path.parent / "container.log").read_text() == "first capture"
    assert len(r.log_calls) == 1


async def test_dump_container_log_skips_when_logs_empty():
    r = _LogRuntime(logs_text="")
    h = await r.launch("r1", "design", 1, {})
    await rt._dump_container_log(r, h)
    assert not (h.result_path.parent / "container.log").exists()


async def test_dump_container_log_truncates_oversized_output(monkeypatch):
    monkeypatch.setattr(rt, "CONTAINER_LOG_MAX_BYTES", 10)
    r = _LogRuntime(logs_text="0123456789ABCDEF")
    h = await r.launch("r1", "design", 1, {})
    await rt._dump_container_log(r, h)
    assert (h.result_path.parent / "container.log").read_text() == "6789ABCDEF"


async def test_reconcile_cancel_captures_container_log():
    """The cancel branch dumps BEFORE the caller's stop() — the container
    still exists at reconcile return."""
    r = _LogRuntime(logs_text="killed mid-run")
    r.script = {"design": ["hung"]}
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await rt.reconcile(r, h, deadline_s=5, cancel_event=ev)
    assert outcome["status"] == rt.CANCELLED
    assert (h.result_path.parent / "container.log").read_text() == "killed mid-run"


async def test_reconcile_timeout_captures_container_log():
    r = _LogRuntime(logs_text="timed out output")
    r.script = {"design": ["hung"]}
    h = await r.launch("r1", "design", 1, {})
    outcome = await rt.reconcile(r, h, deadline_s=0.2)
    assert outcome["status"] == rt.Result.FAILED_TIMEOUT
    assert (h.result_path.parent / "container.log").read_text() == "timed out output"


# ── upload_step_logs ─────────────────────────────────────────────────


RUN_ID = "11111111-2222-3333-4444-555555555555"


class _Store:
    """Duck-typed object store recording put_file calls."""

    def __init__(self, fail_keys=()):
        self.uploads: list[tuple[str, str]] = []
        self.fail_keys = set(fail_keys)

    async def put_file(self, key, path, content_type=None):
        if key in self.fail_keys:
            raise RuntimeError(f"upload boom for {key}")
        self.uploads.append((key, path))


def _stmt_kind(stmt):
    """Extract the RunLog.kind literal from upload_step_logs' select —
    the where clause ANDs four BinaryExpressions, one of which compares
    the kind column against a string literal."""
    for expr in stmt.whereclause.get_children():
        if getattr(getattr(expr, "left", None), "name", None) == "kind":
            return getattr(expr.right, "value", None)
        if getattr(getattr(expr, "right", None), "name", None) == "kind":
            return getattr(expr.left, "value", None)
    return None


class _Session:
    """Duck-typed async session: scalar() resolves the RunLog lookup against
    .existing_kinds (None when absent, like the real DB); add() records."""

    def __init__(self, existing_kinds=()):
        self.existing_kinds = set(existing_kinds)
        self.added = []

    async def scalar(self, stmt):
        kind = _stmt_kind(stmt)
        if kind in self.existing_kinds:
            return SimpleNamespace(kind=kind)
        return None

    def add(self, obj):
        self.added.append(obj)


class _Run:
    id = RUN_ID


async def _attempt_with_logs(files: dict[str, bytes]):
    """Launch a FakeRuntime attempt dir and drop the given log files into it."""
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})
    for name, data in files.items():
        (h.result_path.parent / name).write_bytes(data)
    return h


async def test_upload_step_logs_uploads_each_kind_and_adds_rows():
    h = await _attempt_with_logs({
        "agent.log": b"agent stuff",
        "container.log": b"container stuff",
        "diagnostics.txt": b"diag stuff",
    })
    store = _Store()
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, h, store)
    assert added == 3
    assert [k for k, _ in store.uploads] == [
        f"logs/{RUN_ID}/design/1/agent.log",
        f"logs/{RUN_ID}/design/1/container.log",
        f"logs/{RUN_ID}/design/1/diagnostics.txt",
    ]
    by_kind = {row.kind: row for row in session.added}
    assert set(by_kind) == {"agent", "container", "diagnostics"}
    row = by_kind["agent"]
    assert row.object_key == f"logs/{RUN_ID}/design/1/agent.log"
    assert row.size_bytes == len(b"agent stuff")
    assert row.run_id == RUN_ID
    assert row.step_id == "design"
    assert row.attempt_no == 1


async def test_upload_skips_missing_and_empty_files():
    h = await _attempt_with_logs({
        "agent.log": b"",                      # empty → skip
        "container.log": b"only container",    # diagnostics.txt absent → skip
    })
    store = _Store()
    added = await upload_step_logs(_Session(), _Run(), "design", 1, h, store)
    assert added == 1
    assert [k for k, _ in store.uploads] == [f"logs/{RUN_ID}/design/1/container.log"]


async def test_upload_none_store_returns_zero():
    h = await _attempt_with_logs({"agent.log": b"x"})
    assert await upload_step_logs(_Session(), _Run(), "design", 1, h, None) == 0


async def test_upload_failed_put_does_not_stop_other_kinds():
    """A failed put is logged and skipped — the other kinds still upload and
    the step's transaction still commits."""
    h = await _attempt_with_logs({
        "agent.log": b"a",
        "container.log": b"c",
    })
    store = _Store(fail_keys={f"logs/{RUN_ID}/design/1/agent.log"})
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, h, store)
    assert added == 1
    assert [k for k, _ in store.uploads] == [f"logs/{RUN_ID}/design/1/container.log"]
    assert [row.kind for row in session.added] == ["container"]


async def test_upload_does_not_duplicate_existing_row():
    """Crash re-entry: the reference row already exists — the re-upload is
    idempotent and no second row is added."""
    h = await _attempt_with_logs({
        "agent.log": b"a",
        "container.log": b"c",
        "diagnostics.txt": b"d",
    })
    store = _Store()
    session = _Session(existing_kinds={"container"})
    added = await upload_step_logs(session, _Run(), "design", 1, h, store)
    assert added == 3                       # all three ensured...
    assert [row.kind for row in session.added] == ["agent", "diagnostics"]
