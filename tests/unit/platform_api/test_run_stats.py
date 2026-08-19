"""Unit — run-stat counters behind the Workflows catalog and workflow home.

``_visit_stream`` mirrors ``runs._build_timeline``'s visit-opening block (the
CANONICAL implementation) minus the display concerns. Differential tests feed
the same transition stream to both and pin the ``(step_id, visit_no)``
sequences together, so the mirror can never drift silently.

``_run_stats`` turns that stream into the catalog/home counters: executions,
loop-backs (re-visits — human send-backs and workflow-routed loops alike),
loop edges, duration (terminal run-level ts − init run-level ts), and gate
waits (decision ts − gate-open ts).

Row shape mirrors run 03ad1cd6's actual stream from test_timeline_builder.py,
including the run-level bookkeeping rows that must be skipped.
"""

import time
from types import SimpleNamespace

from platform_api.routers._run_stats import (
    _median,
    _relative_time,
    _run_stats,
    _visit_stream,
)
from platform_api.routers.runs import _build_timeline

WORKFLOW = [
    {"id": "story-design", "skill": "story-design", "label": "Design the story",
     "model": "high", "deadline": 900},
    {"id": "implement", "skill": "implement", "label": "Implement",
     "model": "medium", "deadline": 1800},
    {"id": "code-review", "skill": "code-review", "label": "Review code",
     "model": "high", "deadline": 900},
    {"id": "pr-create", "skill": "pr-create", "label": "Open PR",
     "model": "low", "deadline": 600},
]

GATES = {"story-design": {"review": "required", "role": "any"}}


def _row(**kw):
    """Duck-typed transition row with Transition's column defaults."""
    base = {"step_id": None, "from_state": "", "to_state": "", "ts": 0.0,
            "result_status": None, "actor": "system", "reason": None,
            "payload": None, "attempt_no": 1}
    base.update(kw)
    return SimpleNamespace(**base)


def _stream():
    """Run-level rows + gated design + a workflow-routed re-loop + a halt row.

    Visits in execution order: story-design(1), implement(1), code-review(1),
    implement(2) [re-loop], code-review(2) [halt].
    """
    return [
        _row(from_state="pending", to_state="running", ts=0.0,
             reason="branch created: feat/x"),
        # story-design visit 1 — gated, approved after a 599s wait
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=100.0, reason="running skill story-design"),
        _row(step_id="story-design", from_state="running", to_state="awaiting_result",
             ts=101.0, reason="container launched"),
        _row(step_id="story-design", from_state="awaiting_result", to_state="completed",
             ts=400.0, result_status="completed", reason="ok",
             payload={"summary": "Story done", "commit": "abc1234"}),
        _row(step_id="story-design", from_state="completed", to_state="awaiting_approval",
             ts=401.0, result_status="completed",
             payload={"result_status": "completed", "role": "lead"}),
        _row(step_id="story-design", from_state="awaiting_approval", to_state="completed",
             ts=1000.0, result_status="completed", actor="ksaraav@gmail.com",
             reason="reviewer chose: approve — Approved", payload=None),
        # implement visit 1 — completed, then verified downstream
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=1000.0, reason="running skill implement"),
        # crash-recovery resume: the SAME open visit re-announced — must merge
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=1001.0, reason="running skill implement"),
        _row(step_id="implement", from_state="running", to_state="awaiting_result",
             ts=1002.0, reason="container launched"),
        _row(step_id="implement", from_state="awaiting_result", to_state="completed",
             ts=1400.0, result_status="completed", reason="ok",
             payload={"commit": "def5678"}),
        # code-review visit 1 — wants changes → routes back to implement
        _row(step_id="code-review", from_state="pending", to_state="running",
             ts=1400.0, reason="running skill code-review"),
        _row(step_id="code-review", from_state="running", to_state="awaiting_result",
             ts=1401.0, reason="container launched"),
        _row(step_id="code-review", from_state="awaiting_result", to_state="failed",
             ts=1800.0, result_status="changes_requested", reason="ok", payload=None),
        # implement visit 2 — the re-loop
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=1800.0, reason="running skill implement"),
        _row(step_id="implement", from_state="running", to_state="awaiting_result",
             ts=1801.0, reason="container launched"),
        _row(step_id="implement", from_state="awaiting_result", to_state="failed",
             ts=1802.0, result_status="failed_init",
             reason="could not clone https://github.com/saraavrnd/learn-portal.git @ main",
             payload={}),
        # the engine's routing row — same step, no new visit
        _row(step_id="implement", from_state="running", to_state="failed",
             ts=1802.0, result_status="failed_init",
             reason="workflow has no route for 'failed_init' from step 'implement'",
             payload={}),
        # runaway-loop cap: failure row addressed to a step that is NOT the
        # open visit — surfaces as a halted visit in both builders
        _row(step_id="code-review", from_state="running", to_state="failed",
             ts=1803.0, result_status=None,
             reason="step 'code-review' visited 4 times in one dispatch (cap 3) — runaway loop halted, escalating for a human",
             payload={}),
        # run-level terminal row
        _row(from_state="running", to_state="failed", ts=1900.0,
             reason="run failed"),
    ]


def _reannounce_stream():
    """Retries/resumes merge into one visit (test_timeline_builder's shape)."""
    return [
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=100.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="running", to_state="awaiting_result",
             ts=101.0, reason="container launched"),
        _row(step_id="test-creator", from_state="awaiting_result",
             to_state="RETRYING", ts=250.0, result_status="failed_timeout",
             reason="transient 'failed_timeout' — retrying in a fresh container"),
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=251.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=500.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=800.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="awaiting_result",
             to_state="completed", ts=900.0, result_status="completed",
             reason="ok", payload={}),
    ]


def _self_loop_stream():
    """Same step after a CLOSED visit opens a new visit (a real self-loop)."""
    return [
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=100.0),
        _row(step_id="story-design", from_state="awaiting_result",
             to_state="completed", ts=200.0, result_status="changes_requested",
             reason="ok", payload={}),
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=300.0),
        _row(step_id="story-design", from_state="awaiting_result",
             to_state="completed", ts=400.0, result_status="completed",
             reason="ok", payload={}),
    ]


# ── Differential: the mirror must agree with the canonical builder ──────────


def _assert_mirrors(rows, run_state):
    """Both builders must emit identical (step_id, visit_no, started_ts)."""
    visits = _visit_stream(rows)
    nodes = [n for n in _build_timeline(rows, WORKFLOW, GATES,
                                        run_state=run_state)["nodes"]
             if n["visit_no"] > 0]
    assert [(v["step_id"], v["visit_no"]) for v in visits] == [
        (n["step_id"], n["visit_no"]) for n in nodes]
    assert [v["started_ts"] for v in visits] == [n["started_ts"] for n in nodes]
    return visits


def test_mirror_on_loop_back_and_halt_stream():
    visits = _assert_mirrors(_stream(), run_state="failed")
    assert [(v["step_id"], v["visit_no"]) for v in visits] == [
        ("story-design", 1), ("implement", 1), ("code-review", 1),
        ("implement", 2), ("code-review", 2),
    ]


def test_mirror_on_reannouncement_stream():
    visits = _assert_mirrors(_reannounce_stream(), run_state="running")
    assert [(v["step_id"], v["visit_no"]) for v in visits] == [
        ("test-creator", 1),
    ]


def test_mirror_on_self_loop_stream():
    visits = _assert_mirrors(_self_loop_stream(), run_state="completed")
    assert [(v["step_id"], v["visit_no"]) for v in visits] == [
        ("story-design", 1), ("story-design", 2),
    ]


# ── _run_stats: hand-computed counters ──────────────────────────────────────


def test_run_stats_hand_computed():
    stats = _run_stats(_stream(), created_ts=50.0)
    assert stats["executions"] == 5
    assert stats["loop_backs"] == 2
    assert dict(stats["loop_edges"]) == {
        ("code-review", "implement"): 1,
        ("implement", "code-review"): 1,
    }
    # duration = terminal run-level ts − init run-level ts
    assert stats["duration_s"] == 1900.0
    # gate wait = decision ts − gate-open ts (story-design: 1000 − 401)
    assert stats["gate_waits"] == [599.0]


def test_run_stats_duration_falls_back_to_created_ts_without_init_row():
    rows = [r for r in _stream() if not (
        r.step_id is None and r.from_state == "pending"
        and r.to_state == "running")]
    stats = _run_stats(rows, created_ts=50.0)
    assert stats["duration_s"] == 1900.0 - 50.0


def test_run_stats_duration_none_without_terminal_row():
    rows = [r for r in _stream() if r.to_state not in
            ("failed",) or r.step_id is not None]
    stats = _run_stats(rows, created_ts=50.0)
    assert stats["duration_s"] is None
    # Counting still works without a terminal row.
    assert stats["executions"] == 5


def test_cancel_close_does_not_count_as_a_gate_wait():
    """A gate closed by stop-run is not a human decision — no wait is
    recorded (the wait only surfaces as the decision edge)."""
    rows = [
        _row(from_state="pending", to_state="running", ts=100.0),
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=110.0),
        _row(step_id="story-design", from_state="awaiting_result",
             to_state="completed", ts=400.0, result_status="completed",
             reason="ok", payload={"commit": "abc1234"}),
        _row(step_id="story-design", from_state="completed", to_state="awaiting_approval",
             ts=401.0, result_status="completed",
             payload={"result_status": "completed", "role": "lead"}),
        _row(step_id="story-design", from_state="awaiting_approval", to_state="completed",
             ts=900.0, result_status="completed", actor="ksaraav@gmail.com",
             reason="gate closed — run cancelled by ksaraav@gmail.com",
             payload=None),
        _row(from_state="running", to_state="cancelled", ts=1000.0),
    ]
    stats = _run_stats(rows, created_ts=0.0)
    assert stats["gate_waits"] == []
    assert stats["duration_s"] == 900.0
    assert stats["executions"] == 1


def test_gate_wait_uses_decision_ts():
    """Approve and send-back decisions both count the wait the same way."""
    rows = [
        _row(from_state="pending", to_state="running", ts=0.0),
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=10.0),
        _row(step_id="story-design", from_state="completed", to_state="awaiting_approval",
             ts=500.0, payload={"role": "lead"}),
        _row(step_id="story-design", from_state="awaiting_approval", to_state="completed",
             ts=2000.0, actor="a@b.c",
             reason="reviewer chose: send back to story-design — rework the brief"),
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=2000.0),
        _row(step_id="story-design", from_state="completed", to_state="awaiting_approval",
             ts=2400.0, payload={"role": "lead"}),
        _row(step_id="story-design", from_state="awaiting_approval", to_state="completed",
             ts=2500.0, actor="a@b.c", reason="reviewer chose: approve — ok"),
        _row(from_state="running", to_state="completed", ts=2600.0),
    ]
    stats = _run_stats(rows, created_ts=0.0)
    # The send-back visit re-opens the same step → visit 2 (a loop-back).
    assert stats["executions"] == 2
    assert stats["loop_backs"] == 1
    assert stats["gate_waits"] == [1500.0, 100.0]


# ── _median / _relative_time ────────────────────────────────────────────────


def test_median():
    assert _median([]) is None
    assert _median([5.0]) == 5.0
    assert _median([3.0, 1.0, 2.0]) == 2.0
    assert _median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_relative_time():
    now = time.time()
    assert _relative_time(None) is None
    assert _relative_time(now) == "just now"
    assert _relative_time(now - 195) == "3m ago"
    assert _relative_time(now - 2 * 3600) == "2h ago"
    assert _relative_time(now - 3 * 86400) == "3d ago"
