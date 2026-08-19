"""Unit — container-log capture + object-store registration (ADR-011/ADR-014).

The capture must happen while the container still exists (docker logs die
with it), so reconcile's kill paths (cancel/timeout) capture first and every
other terminal return is covered by the state machine's own capture. Both
the capture and the log registration are best-effort: a failed put or head
must never fail the step.
"""

import asyncio
from types import SimpleNamespace

import pytest
from bheembhai.log_keys import log_key
from bheembhai.providers.local_storage import LocalStorage

import engine_service.runtime as rt
from conftest import FakeRuntime
from engine_service.log_upload import upload_step_logs


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


# ── _capture_container_log ────────────────────────────────────────────


def _store(tmp_path) -> LocalStorage:
    return LocalStorage(base_path=str(tmp_path / "artifacts"))


async def test_capture_container_log_writes_to_store(tmp_path):
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="line a\nline b\n", store=store)
    h = await r.launch("r1", "design", 1, {})
    await rt._capture_container_log(store, r, h)
    obj = await store.get(log_key("r1", "design", 1, "container"))
    assert obj.data == b"line a\nline b\n"
    assert r.log_calls == [(h, rt.CONTAINER_LOG_TAIL_LINES)]


async def test_capture_container_log_idempotent_when_object_exists(tmp_path):
    """A second capture must not clobber the first (reconcile already
    captured it on its own kill path before the state machine's blanket
    capture) — the head-check skips the second docker logs call entirely."""
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="first capture", store=store)
    h = await r.launch("r1", "design", 1, {})
    await rt._capture_container_log(store, r, h)
    r.logs_text = "second capture"
    await rt._capture_container_log(store, r, h)
    obj = await store.get(log_key("r1", "design", 1, "container"))
    assert obj.data == b"first capture"
    assert len(r.log_calls) == 1


async def test_capture_container_log_skips_when_logs_empty(tmp_path):
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="", store=store)
    h = await r.launch("r1", "design", 1, {})
    await rt._capture_container_log(store, r, h)
    assert await store.get(log_key("r1", "design", 1, "container")) is None


async def test_capture_container_log_truncates_oversized_output(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "CONTAINER_LOG_MAX_BYTES", 10)
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="0123456789ABCDEF", store=store)
    h = await r.launch("r1", "design", 1, {})
    await rt._capture_container_log(store, r, h)
    obj = await store.get(log_key("r1", "design", 1, "container"))
    assert obj.data == b"6789ABCDEF"


async def test_reconcile_cancel_captures_container_log(tmp_path):
    """The cancel branch captures BEFORE the caller's stop() — the container
    still exists at reconcile return."""
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="killed mid-run", store=store)
    r.script = {"design": ["hung"]}
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await rt.reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)
    assert outcome["status"] == rt.CANCELLED
    obj = await store.get(log_key("r1", "design", 1, "container"))
    assert obj.data == b"killed mid-run"


async def test_reconcile_timeout_captures_container_log(tmp_path):
    store = _store(tmp_path)
    r = _LogRuntime(logs_text="timed out output", store=store)
    r.script = {"design": ["hung"]}
    h = await r.launch("r1", "design", 1, {})
    outcome = await rt.reconcile(r, h, deadline_s=0.2, store=store)
    assert outcome["status"] == rt.Result.FAILED_TIMEOUT
    obj = await store.get(log_key("r1", "design", 1, "container"))
    assert obj.data == b"timed out output"


# ── upload_step_logs (registration-only) ─────────────────────────────


RUN_ID = "11111111-2222-3333-4444-555555555555"


class _Store:
    """Duck-typed object store: pre-seeded objects served by head(), put_file
    calls recorded so tests can assert no data movement happens (ADR-014 —
    the agents/engine upload to the final keys, the engine only registers)."""

    def __init__(self, objects=None, head_boom=()):
        self.objects = dict(objects or {})
        self.head_boom = set(head_boom)
        self.puts = []

    async def head(self, key):
        if key in self.head_boom:
            raise RuntimeError(f"head boom for {key}")
        data = self.objects.get(key)
        return None if data is None else SimpleNamespace(size=len(data))

    async def put_file(self, key, path, content_type=None):
        self.puts.append((key, path))


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


def _attempt_with_logs(files: dict[str, bytes]) -> _Store:
    """Seed a store with the given log artifacts at the attempt's final keys."""
    store = _Store()
    for kind, data in files.items():
        store.objects[log_key(RUN_ID, "design", 1, kind)] = data
    return store


async def test_upload_step_logs_registers_each_kind_and_adds_rows():
    store = _attempt_with_logs({
        "agent": b"agent stuff",
        "container": b"container stuff",
        "diagnostics": b"diag stuff",
    })
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, store)
    assert added == 3
    assert store.puts == []                    # registration moves no data
    by_kind = {row.kind: row for row in session.added}
    assert set(by_kind) == {"agent", "container", "diagnostics"}
    row = by_kind["agent"]
    assert row.object_key == f"logs/{RUN_ID}/design/1/agent.log"
    assert row.size_bytes == len(b"agent stuff")
    assert row.run_id == RUN_ID
    assert row.step_id == "design"
    assert row.attempt_no == 1


async def test_upload_skips_missing_and_empty_objects():
    store = _attempt_with_logs({
        "agent": b"",                  # empty → skip
        "container": b"only container",  # diagnostics absent → skip
    })
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, store)
    assert added == 1
    assert [row.kind for row in session.added] == ["container"]


async def test_upload_none_store_returns_zero():
    assert await upload_step_logs(_Session(), _Run(), "design", 1, None) == 0


async def test_upload_absent_object_adds_no_row():
    """No artifact, no reference row — the platform's logs endpoint would
    404 a stale pointer otherwise."""
    store = _attempt_with_logs({})
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, store)
    assert added == 0
    assert session.added == []


async def test_upload_head_failure_skips_kind_and_keeps_going():
    """A failed head is logged and skipped — the other kinds still register
    and the step's transaction still commits."""
    store = _attempt_with_logs({
        "agent": b"a",
        "container": b"c",
    })
    store.head_boom = {log_key(RUN_ID, "design", 1, "agent")}
    session = _Session()
    added = await upload_step_logs(session, _Run(), "design", 1, store)
    assert added == 1
    assert [row.kind for row in session.added] == ["container"]


async def test_upload_does_not_duplicate_existing_row():
    """Crash re-entry: the reference row already exists — the re-registration
    is idempotent and no second row is added."""
    store = _attempt_with_logs({
        "agent": b"a",
        "container": b"c",
        "diagnostics": b"d",
    })
    session = _Session(existing_kinds={"container"})
    added = await upload_step_logs(session, _Run(), "design", 1, store)
    assert added == 3                       # all three ensured...
    assert [row.kind for row in session.added] == ["agent", "diagnostics"]
