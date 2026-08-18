"""Unit tests — runtime protocol + reconcile classification matrix (no Docker needed)."""

import json
import sys
import types

import pytest

import engine_service.runtime as rt
from conftest import FakeRuntime
from engine_service.runtime import Result, reconcile


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    """Shrink the poll/grace constants so classification tests run in milliseconds."""
    monkeypatch.setattr(rt, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(rt, "GRACE_SECONDS", 0.05)



async def test_reconcile_completed():
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.COMPLETED
    assert outcome["cost_usd"] == 0.01
    assert outcome["summary"] == "design done"
    # Pre-flag agents (no cost_reported key) still count: a non-zero figure
    # implies the CLI reported it — real spend never reads "unknown".
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is False


async def test_reconcile_zero_cost_with_flag_stays_reported():
    """An explicit cost_reported:true on a $0.00 session is honest reporting,
    not "unknown" — e.g. a mock run that really did spend nothing."""
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})
    h.result_path.write_text(json.dumps(
        {"status": "completed", "cost_usd": 0, "cost_reported": True}))
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is True


async def test_reconcile_zero_cost_without_flag_reads_unknown():
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})
    h.result_path.write_text(json.dumps({"status": "completed", "cost_usd": 0}))
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is False



async def test_scrape_partial_cost_reads_terminal_result_event(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_text(json.dumps({"type": "result", "total_cost_usd": 0.42}) + "\n")
    assert await rt._scrape_partial_cost(log_path) == 0.42


async def test_scrape_partial_cost_last_match_wins(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_text(
        json.dumps({"type": "assistant", "total_cost_usd": 0.10}) + "\n"
        + json.dumps({"type": "result", "total_cost_usd": 0.99}) + "\n")
    assert await rt._scrape_partial_cost(log_path) == 0.99


async def test_scrape_partial_cost_missing_file_is_none(tmp_path):
    assert await rt._scrape_partial_cost(tmp_path / "nope.log") is None


async def test_scrape_partial_cost_no_cost_event_is_none(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_text('{"type": "system", "subtype": "init"}\nnot json\n')
    assert await rt._scrape_partial_cost(log_path) is None


async def test_scrape_partial_cost_rejects_negative(tmp_path):
    log_path = tmp_path / "agent.log"
    log_path.write_text(json.dumps({"type": "result", "total_cost_usd": -1.5}) + "\n")
    assert await rt._scrape_partial_cost(log_path) is None


async def test_reconcile_cancel_recovers_partial_cost_from_log():
    """A kill lands mid-session — whatever the CLI reported before dying must
    still count, flagged partial (the session would have spent more)."""
    import asyncio
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    (h.result_path.parent / "agent.log").write_text(
        json.dumps({"type": "result", "total_cost_usd": 1.25}) + "\n")
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev)
    assert outcome["status"] == rt.CANCELLED
    assert outcome["cost_usd"] == 1.25
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is True


async def test_reconcile_cancel_without_log_marks_cost_unknown():
    import asyncio
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev)
    assert outcome["status"] == rt.CANCELLED
    assert outcome["cost_usd"] == 0
    assert outcome["cost_reported"] is False
    assert outcome["cost_partial"] is False


async def test_reconcile_timeout_recovers_partial_cost_before_cleanup():
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    (h.result_path.parent / "agent.log").write_text(
        json.dumps({"type": "result", "total_cost_usd": 0.55}) + "\n")
    outcome = await reconcile(r, h, deadline_s=0.2)
    assert outcome["status"] == Result.FAILED_TIMEOUT
    assert outcome["cost_usd"] == 0.55
    assert outcome["cost_reported"] is True
    assert outcome["cost_partial"] is True



async def test_reconcile_exit_nonzero_downgrades_completed():
    r = FakeRuntime({"design": ["exit-nonzero"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.FAILED_EXECUTION



async def test_reconcile_nonzero_exit_keeps_domain_status():
    """Only a *completed* payload is downgraded on a bad exit — a BLOCK verdict with a
    crashing container stays BLOCK (the verdict is what matters, the exit is noise)."""
    r = FakeRuntime({"design": ["block"]})
    h = await r.launch("r1", "design", 1, {})
    # force the container to look like it crashed after writing the payload
    h.behaviour = "crash"
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.BLOCK
    assert outcome["next_hint"] == "implement"



async def test_reconcile_gone_is_infra_failure():
    r = FakeRuntime({"design": ["vanish"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.FAILED_INFRA
    assert "vanished" in outcome["reason"]



async def test_reconcile_exited_without_result_is_incomplete():
    r = FakeRuntime({"design": ["silent"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.FAILED_INCOMPLETE



async def test_reconcile_timeout_cleans_up():
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=0.2)
    assert outcome["status"] == Result.FAILED_TIMEOUT
    assert h in r.cleaned   # deadline cleanup, not just on happy paths



async def test_reconcile_status_error_is_infra_failure():
    r = FakeRuntime({"design": ["error"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=5)
    assert outcome["status"] == Result.FAILED_INFRA



async def test_reconcile_cancel_event_aborts_immediately():
    """A set cancel event aborts reconcile with the CANCELLED sentinel even
    while the container is still running — the caller owns stopping it."""
    import asyncio
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev)
    assert outcome["status"] == rt.CANCELLED
    assert h not in r.cleaned    # cancel path is stop(), not cleanup()



async def test_reconcile_cancel_event_set_midrun_aborts():
    """The event is checked every poll tick — a stop request lands within one
    POLL_INTERVAL while the container keeps reporting 'running'."""
    import asyncio
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    ev = asyncio.Event()

    async def _press_stop():
        await asyncio.sleep(0.05)
        ev.set()

    async def _watch():
        return await reconcile(r, h, deadline_s=5, cancel_event=ev)

    watch = asyncio.create_task(_watch())
    press = asyncio.create_task(_press_stop())
    outcome = await watch
    await press
    assert outcome["status"] == rt.CANCELLED
    assert h not in r.cleaned



async def test_reconcile_cancel_beats_published_result():
    """The event check leads the loop: even an already-published result cannot
    beat a set cancel event (the user pressed stop — that step's push stands,
    the run ends cancelled)."""
    import asyncio
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})   # payload written at launch
    ev = asyncio.Event()
    ev.set()
    outcome = await reconcile(r, h, deadline_s=5, cancel_event=ev)
    assert outcome["status"] == rt.CANCELLED



async def test_reconcile_without_cancel_event_never_aborts():
    """cancel_event=None (crash-recovery resume path) behaves as before."""
    r = FakeRuntime({"design": ["hung"]})
    h = await r.launch("r1", "design", 1, {})
    outcome = await reconcile(r, h, deadline_s=0.2, cancel_event=None)
    assert outcome["status"] == Result.FAILED_TIMEOUT



async def test_reconcile_reports_progress_changes():
    r = FakeRuntime()
    h = await r.launch("r1", "design", 1, {})
    progress = {"phase": "writing", "note": "draft 1", "elapsed_s": 4}
    (h.result_path.parent / "progress.json").write_text(json.dumps(progress))
    seen = []
    await reconcile(r, h, deadline_s=5, on_progress=lambda p: seen.append(p))
    assert progress in seen



async def test_fake_runtime_writes_result_to_correct_path():
    r = FakeRuntime({"design": ["ok"]})
    h = await r.launch("run-42", "design", 2, {"RUN_ID": "run-42"})
    payload = json.loads(h.result_path.read_text())
    assert payload["status"] == "completed"
    assert r.calls == [("design", 2)]
    assert r.contexts == [("design", None)]



class _FakeContainers:
    def run(self, *args, **kwargs):
        class _C:
            id = "f" * 12
        return _C()


class _FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainers()


async def test_launch_clears_stale_result(monkeypatch, tmp_path):
    """A launch into a reused attempt dir must not inherit the previous attempt's
    result file — the reconciler reads its presence as "published" (regression for
    the implement re-loop, where poll #1 saw result_present=True from the stale file
    and a crash-before-publish relaunch could be classified with the old payload)."""
    # DockerRuntime imports docker lazily inside __init__; CI installs only
    # shared/[dev] (no docker-py), so string-patching `docker.from_env` would make
    # monkeypatch's resolve() import the real module and fail. Seed a stub instead.
    docker_stub = types.ModuleType("docker")
    docker_stub.from_env = lambda **kw: _FakeDockerClient()
    docker_stub.DockerClient = lambda **kw: _FakeDockerClient()
    docker_stub.errors = types.SimpleNamespace(NotFound=Exception)
    monkeypatch.setitem(sys.modules, "docker", docker_stub)
    r = rt.DockerRuntime(image="test-image", workdir=str(tmp_path))
    outdir = tmp_path / "results" / "r1" / "implement" / "1"
    outdir.mkdir(parents=True)
    stale = outdir / rt.RESULT_FILENAME
    stale.write_text('{"status": "completed"}')  # previous attempt's payload
    h = await r.launch("r1", "implement", 1, {})
    assert not stale.exists()
    assert h.result_path == stale
