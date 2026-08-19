"""Unit tests — runtime protocol + reconcile classification matrix (no Docker needed)."""

import json
import sys
import types

import pytest
from bheembhai.log_keys import log_key, progress_key, result_key
from bheembhai.providers.local_storage import LocalStorage

import engine_service.runtime as rt
from conftest import FakeRuntime
from engine_service.runtime import Result, reconcile


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    """Shrink the poll/grace constants so classification tests run in milliseconds."""
    monkeypatch.setattr(rt, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rt, "GRACE_SECONDS", 0.05)


def _store(tmp_path) -> LocalStorage:
    return LocalStorage(base_path=str(tmp_path / "artifacts"))


async def _run(store, script=None, *, deadline_s=5, **kw):
    """Drive one reconcile with a fresh FakeRuntime wired to the same store."""
    r = FakeRuntime(script, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=deadline_s, store=store, **kw)
    return r, h, outcome


async def test_reconcile_completed(tmp_path):
    store = _store(tmp_path)
    _, _, outcome = await _run(store)
    assert outcome["status"] == Result.COMPLETED
    assert outcome["cost_usd"] == 0.01
    assert outcome["summary"] == "design done"
    # Pre-flag agents (no cost_reported key) still count: a non-zero figure
    # implies the CLI reported it — real spend never reads "unknown".
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is False


async def test_reconcile_zero_cost_with_flag_stays_reported(tmp_path):
    """An explicit cost_reported:true on a $0.00 session is honest reporting,
    not "unknown" — e.g. a mock run that really did spend nothing."""
    store = _store(tmp_path)
    await store.put(result_key("r1", "design", 1),
                    json.dumps({"status": "completed", "cost_usd": 0,
                                "cost_reported": True}).encode(),
                    content_type="application/json")
    r = FakeRuntime()   # no store — launch must not overwrite the seeded payload
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is True


async def test_reconcile_zero_cost_without_flag_reads_unknown(tmp_path):
    store = _store(tmp_path)
    await store.put(result_key("r1", "design", 1),
                    json.dumps({"status": "completed", "cost_usd": 0}).encode(),
                    content_type="application/json")
    r = FakeRuntime()   # no store — launch must not overwrite the seeded payload
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is False



async def test_scrape_partial_cost_reads_terminal_result_event(tmp_path):
    store = _store(tmp_path)
    await store.put(log_key("r1", "design", 1, "agent"),
                    (json.dumps({"type": "result", "total_cost_usd": 0.42}) + "\n").encode(),
                    content_type="text/plain")
    assert await rt._scrape_partial_cost(store, log_key("r1", "design", 1, "agent")) == 0.42


async def test_scrape_partial_cost_last_match_wins(tmp_path):
    store = _store(tmp_path)
    await store.put(
        log_key("r1", "design", 1, "agent"),
        (json.dumps({"type": "assistant", "total_cost_usd": 0.10}) + "\n"
         + json.dumps({"type": "result", "total_cost_usd": 0.99}) + "\n").encode(),
        content_type="text/plain")
    assert await rt._scrape_partial_cost(store, log_key("r1", "design", 1, "agent")) == 0.99


async def test_scrape_partial_cost_missing_object_is_none(tmp_path):
    store = _store(tmp_path)
    assert await rt._scrape_partial_cost(store, log_key("r1", "design", 1, "agent")) is None


async def test_scrape_partial_cost_no_cost_event_is_none(tmp_path):
    store = _store(tmp_path)
    await store.put(log_key("r1", "design", 1, "agent"),
                    b'{"type": "system", "subtype": "init"}\nnot json\n',
                    content_type="text/plain")
    assert await rt._scrape_partial_cost(store, log_key("r1", "design", 1, "agent")) is None


async def test_scrape_partial_cost_rejects_negative(tmp_path):
    store = _store(tmp_path)
    await store.put(log_key("r1", "design", 1, "agent"),
                    (json.dumps({"type": "result", "total_cost_usd": -1.5}) + "\n").encode(),
                    content_type="text/plain")
    assert await rt._scrape_partial_cost(store, log_key("r1", "design", 1, "agent")) is None


async def test_reconcile_cancel_recovers_partial_cost_from_log(tmp_path):
    """A kill lands mid-session — whatever the CLI reported before dying must
    still count, flagged partial (the session would have spent more)."""
    import asyncio
    store = _store(tmp_path)
    await store.put(log_key("r1", "design", 1, "agent"),
                    (json.dumps({"type": "result", "total_cost_usd": 1.25}) + "\n").encode(),
                    content_type="text/plain")
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)
    assert outcome["status"] == rt.CANCELLED
    assert outcome["cost_usd"] == 1.25
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is True


async def test_reconcile_cancel_without_log_marks_cost_unknown(tmp_path):
    import asyncio
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)
    assert outcome["status"] == rt.CANCELLED
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is False
    assert outcome["cost_partial"] is False


async def test_reconcile_timeout_recovers_partial_cost_before_cleanup(tmp_path):
    store = _store(tmp_path)
    await store.put(log_key("r1", "design", 1, "agent"),
                    (json.dumps({"type": "result", "total_cost_usd": 0.55}) + "\n").encode(),
                    content_type="text/plain")
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=0.2, store=store)
    assert outcome["status"] == Result.FAILED_TIMEOUT
    assert outcome["cost_usd"] == 0.55
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is True



async def test_reconcile_exit_nonzero_downgrades_completed(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["exit-nonzero"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["status"] == Result.FAILED_EXECUTION



async def test_reconcile_nonzero_exit_keeps_domain_status(tmp_path):
    """Only a *completed* payload is downgraded on a bad exit — a BLOCK verdict with a
    crashing container stays BLOCK (the verdict is what matters, the exit is noise)."""
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["block"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    # force the container to look like it crashed after writing the payload
    h.behaviour = "crash"
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["status"] == Result.BLOCK
    assert outcome["next_hint"] == "implement"



async def test_reconcile_gone_is_infra_failure(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["vanish"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["status"] == Result.FAILED_INFRA
    assert "vanished" in outcome["reason"]



async def test_reconcile_exited_without_result_is_incomplete(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["silent"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["status"] == Result.FAILED_INCOMPLETE



async def test_reconcile_timeout_cleans_up(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=0.2, store=store)
    assert outcome["status"] == Result.FAILED_TIMEOUT
    assert h in r.cleaned   # deadline cleanup, not just on happy paths



async def test_reconcile_status_error_is_infra_failure(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["error"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5, store=store)
    assert outcome["status"] == Result.FAILED_INFRA



async def test_reconcile_cancel_event_aborts_immediately(tmp_path):
    """A set cancel event aborts reconcile with the CANCELLED sentinel even
    while the container is still running — the caller owns stopping it."""
    import asyncio
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)
    assert outcome["status"] == rt.CANCELLED
    assert h not in r.cleaned    # cancel path is stop(), not cleanup()



async def test_reconcile_cancel_event_set_midrun_aborts(tmp_path):
    """The event is checked every poll tick — a stop request lands within one
    POLL_INTERVAL while the container keeps reporting 'running'."""
    import asyncio
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()

    async def _press_stop():
        await asyncio.sleep(0.05)
        ev.set()

    async def _watch():
        return await reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)

    watch = asyncio.create_task(_watch())
    press = asyncio.create_task(_press_stop())
    outcome = await watch
    await press
    assert outcome["status"] == rt.CANCELLED
    assert h not in r.cleaned



async def test_reconcile_cancel_beats_published_result(tmp_path):
    """The event check leads the loop: even an already-published result cannot
    beat a set cancel event (the user pressed stop — that step's push stands,
    the run ends cancelled)."""
    import asyncio
    store = _store(tmp_path)
    r = FakeRuntime(store=store)
    h = await r.launch("r1", "design", 1, {})   # payload published at launch
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev, store=store)
    assert outcome["status"] == rt.CANCELLED



async def test_reconcile_without_cancel_event_never_aborts(tmp_path):
    """cancel_event=None (crash-recovery resume path) behaves as before."""
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["hung"]}, store=store)
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=0.2, cancel_event=None, store=store)
    assert outcome["status"] == Result.FAILED_TIMEOUT



async def test_reconcile_reports_progress_changes(tmp_path):
    store = _store(tmp_path)
    progress = {"phase": "writing", "note": "draft 1", "elapsed_s": 4}
    await store.put(progress_key("r1", "design", 1), json.dumps(progress).encode(),
                    content_type="application/json")
    r = FakeRuntime(store=store)
    h = await r.launch("r1", "design", 1, {})
    seen = []
    await reconcile(r, h, deadline_s=5, on_progress=lambda p: seen.append(p), store=store)
    assert progress in seen



async def test_fake_runtime_publishes_result_to_object_store(tmp_path):
    store = _store(tmp_path)
    r = FakeRuntime({"design": ["ok"]}, store=store)
    h = await r.launch("run-42", "design", 2, {"RUN_ID": "run-42"})
    payload = json.loads((await store.get(result_key("run-42", "design", 2))).data)
    assert payload["status"] == "completed"
    assert r.calls == [("design", 2)]
    assert r.contexts == [("design", None)]
    # The Handle is key-less (ADR-014) — channels are derived from its identity.
    assert h.run_id == "run-42"
    assert h.step_id == "design"
    assert h.attempt_no == 2



class _RecordingContainers:
    def __init__(self):
        self.calls = []

    def run(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        class _C:
            id = "f" * 12
        return _C()


class _FakeDockerClient:
    def __init__(self):
        self.containers = _RecordingContainers()


async def test_launch_passes_no_volumes_and_image_workdir(monkeypatch, tmp_path):
    """Zero mounts (ADR-014): launch must not pass volumes, and must not even
    take a workdir at construction — the container works in the image-owned
    /workspace. A leftover host-path workdir would make docker silently create
    a root-owned tree the non-root agent cannot write (Phase 1 gotcha)."""
    # DockerRuntime imports docker lazily inside __init__; CI installs only
    # shared/[dev] (no docker-py), so string-patching `docker.from_env` would make
    # monkeypatch's resolve() import the real module and fail. Seed a stub instead.
    docker_stub = types.ModuleType("docker")
    client = _FakeDockerClient()
    docker_stub.from_env = lambda **kw: client
    docker_stub.DockerClient = lambda **kw: _FakeDockerClient()
    docker_stub.errors = types.SimpleNamespace(NotFound=Exception)
    monkeypatch.setitem(sys.modules, "docker", docker_stub)
    r = rt.DockerRuntime(image="test-image")
    h = await r.launch("r1", "implement", 1, {})
    assert h.container_id == "f" * 12
    assert h.run_id == "r1"
    assert h.step_id == "implement"
    assert h.attempt_no == 1
    (_args, kwargs) = client.containers.calls[0]
    assert "volumes" not in kwargs
    assert kwargs["working_dir"] == "/workspace"
