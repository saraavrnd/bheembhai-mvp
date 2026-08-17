"""Unit — the execution timeline rebuilds the true visit order from transitions.

The unique-stage rail lied about loops: when code-review returned
``changes_requested`` and the workflow routed back to ``implement``, the
implement step row showed only the re-run's ``failed_init`` — the first visit
(which test-verify verified) disappeared. ``_build_timeline`` walks the
append-only transition stream: each ``pending→running`` row opens a visit
(attempt_no does NOT distinguish visits — re-loops reuse the same attempt
dir), and visits are emitted in true execution order with their own verdict,
artifacts, and gate decisions.

The row stream below mirrors run 03ad1cd6's actual shape, including the
run-level bookkeeping rows that must be skipped.
"""

from types import SimpleNamespace

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
    base = dict(step_id=None, from_state="", to_state="", ts=0.0,
                result_status=None, actor="system", reason=None,
                payload=None, attempt_no=1)
    base.update(kw)
    return SimpleNamespace(**base)


def _stream():
    """Run 03ad1cd6's execution shape: gated design → implement → code-review
    (changes_requested) → implement again (failed_init), plus run-level rows."""
    return [
        _row(from_state="pending", to_state="running", ts=0.0,
             reason="branch created: feat/x"),
        # story-design visit 1 — gated, approved
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=100.0, reason="running skill story-design"),
        _row(step_id="story-design", from_state="running", to_state="awaiting_result",
             ts=101.0, reason="container launched"),
        _row(step_id="story-design", from_state="awaiting_result", to_state="completed",
             ts=400.0, result_status="completed", reason="ok",
             payload={"summary": "Story done", "commit": "abc1234",
                      "files": [{"path": "docs/story.md"}]}),
        _row(step_id="story-design", from_state="completed", to_state="awaiting_approval",
             ts=401.0, result_status="completed",
             payload={"result_status": "completed"}),
        _row(step_id="story-design", from_state="awaiting_approval", to_state="completed",
             ts=1000.0, result_status="completed", actor="ksaraav@gmail.com",
             reason="reviewer chose: approve — Approved", payload=None),
        # implement visit 1 — completed, then verified downstream
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=1000.0, reason="running skill implement"),
        _row(step_id="implement", from_state="running", to_state="awaiting_result",
             ts=1001.0, reason="container launched"),
        _row(step_id="implement", from_state="awaiting_result", to_state="completed",
             ts=1400.0, result_status="completed", reason="ok",
             payload={"commit": "def5678",
                      "files": [{"path": "src/main.py"}]}),
        # code-review visit 1 — wants changes → routes back to implement
        _row(step_id="code-review", from_state="pending", to_state="running",
             ts=1400.0, reason="running skill code-review"),
        _row(step_id="code-review", from_state="running", to_state="awaiting_result",
             ts=1401.0, reason="container launched"),
        _row(step_id="code-review", from_state="awaiting_result", to_state="failed",
             ts=1800.0, result_status="changes_requested", reason="ok", payload=None),
        # implement visit 2 — the re-loop (attempt_no stays 1, the real bug)
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=1800.0, attempt_no=1, reason="running skill implement"),
        _row(step_id="implement", from_state="running", to_state="awaiting_result",
             ts=1801.0, reason="container launched"),
        _row(step_id="implement", from_state="awaiting_result", to_state="failed",
             ts=1802.0, result_status="failed_init",
             reason="could not clone https://github.com/saraavrnd/learn-portal.git @ main",
             payload={}),
        # the engine's routing-row: reason must NOT replace the clone reason
        _row(step_id="implement", from_state="running", to_state="failed",
             ts=1802.0, result_status="failed_init",
             reason="workflow has no route for 'failed_init' from step 'implement'",
             payload={}),
    ]


def test_timeline_orders_visits_by_execution():
    timeline = _build_timeline(_stream(), WORKFLOW, GATES, run_state="failed")
    nodes = timeline["nodes"]
    assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
        ("story-design", 1), ("implement", 1), ("code-review", 1),
        ("implement", 2), ("pr-create", 0),
    ]


def test_visits_keep_their_own_verdicts():
    nodes = _build_timeline(_stream(), WORKFLOW, GATES, run_state="failed")["nodes"]
    by_key = {(n["step_id"], n["visit_no"]): n for n in nodes}
    # First implement visit still reads completed — not clobbered by the re-run.
    assert by_key[("implement", 1)]["state"] == "done"
    assert by_key[("implement", 1)]["verdict"] == "completed"
    assert [f["path"] for f in by_key[("implement", 1)]["files"]] == ["src/main.py"]
    assert by_key[("implement", 1)]["commit"] == "def5678"
    # The re-run visit carries its own failure, with the clone reason (not the
    # routing-row reason that follows it).
    v2 = by_key[("implement", 2)]
    assert v2["state"] == "failed"
    assert v2["verdict"] == "failed_init"
    assert "could not clone" in v2["reason"]
    # code-review's non-happy verdict is "done" with a verdict chip.
    cr = by_key[("code-review", 1)]
    assert cr["state"] == "done"
    assert cr["verdict"] == "changes_requested"


def test_gate_decision_attaches_to_the_gated_visit():
    nodes = _build_timeline(_stream(), WORKFLOW, GATES, run_state="failed")["nodes"]
    sd = nodes[0]
    assert sd["gate_decision"] == {
        "action": "approve", "actor": "ksaraav@gmail.com",
        "comment": "Approved", "ts": 1000.0,
    }
    # Elapsed stops at the gate card, not at the approval.
    assert sd["elapsed"] == "0:05:01"
    # The visit's payload survives the empty approval row.
    assert sd["summary"] == "Story done"


def test_open_gate_marks_the_visit_awaiting_and_pins_it():
    rows = [r for r in _stream() if not (
        r.step_id == "story-design"
        and r.from_state == "awaiting_approval")]
    timeline = _build_timeline(rows, WORKFLOW, GATES, run_state="paused")
    nodes = timeline["nodes"]
    assert nodes[0]["state"] == "awaiting"
    assert nodes[0]["is_awaiting_review"] is True
    assert timeline["current_node_idx"] == 0
    # An open gate with the run NOT paused is not awaiting (transient states
    # between the gate opening and the pause landing).
    timeline2 = _build_timeline(rows, WORKFLOW, GATES, run_state="running")
    assert timeline2["nodes"][0]["state"] == "done"


def test_pending_tail_and_no_current_node_on_terminal_runs():
    timeline = _build_timeline(_stream(), WORKFLOW, GATES, run_state="failed")
    nodes = timeline["nodes"]
    assert nodes[-1]["step_id"] == "pr-create"
    assert nodes[-1]["state"] == "pending"
    assert nodes[-1]["visit_no"] == 0
    assert timeline["current_node_idx"] is None


def test_retry_and_resume_reannouncements_merge_into_one_visit():
    """A step that never closed can re-fire ``pending→running`` (deadline
    retry via a RETRYING row, or crash-recovery resumes — run b2b1b72a had
    five for test-creator). Each must NOT split a phantom visit: those
    rendered as "running" forever even after the step completed."""
    rows = [
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=100.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="running", to_state="awaiting_result",
             ts=101.0, reason="container launched"),
        # deadline retry: transient row, then the same visit re-announced
        _row(step_id="test-creator", from_state="awaiting_result",
             to_state="RETRYING", ts=250.0, result_status="failed_timeout",
             reason="transient 'failed_timeout' — retrying in a fresh container"),
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=251.0, reason="running skill test-creator"),
        # crash-recovery resumes: bare re-announcements, nothing closed between
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=500.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="pending", to_state="running",
             ts=800.0, reason="running skill test-creator"),
        _row(step_id="test-creator", from_state="awaiting_result",
             to_state="completed", ts=900.0, result_status="completed",
             reason="ok", payload={}),
    ]
    nodes = _build_timeline(rows, WORKFLOW, GATES, run_state="running")["nodes"]
    assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
        ("test-creator", 1), ("story-design", 0), ("implement", 0),
        ("code-review", 0), ("pr-create", 0),
    ]
    v = nodes[0]
    assert v["state"] == "done"
    assert v["verdict"] == "completed"
    assert v["started_ts"] == 100.0    # the original start survives the merges
    assert v["elapsed"] == "0:13:20"


def test_same_step_after_closed_visit_starts_a_new_visit():
    """The merge only applies to OPEN visits: a self-loop (changes_requested →
    same step) after the first visit ended still produces a second visit."""
    rows = [
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
    nodes = _build_timeline(rows, WORKFLOW, GATES, run_state="completed")["nodes"]
    assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
        ("story-design", 1), ("story-design", 2), ("implement", 0),
        ("code-review", 0), ("pr-create", 0),
    ]
    assert nodes[0]["verdict"] == "changes_requested"
    assert nodes[1]["verdict"] == "completed"


def test_runaway_loop_halt_row_surfaces_as_failed_node():
    """The engine's visit-cap halt (run 18a35087 row #110) is a failure row
    addressed to implement AFTER its last visit closed and code-review was
    the open visit — the builder must not drop it, or the run's failure
    reason disappears from the rail."""
    rows = [
        _row(step_id="story-design", from_state="pending", to_state="running",
             ts=100.0),
        _row(step_id="story-design", from_state="running", to_state="awaiting_result",
             ts=101.0),
        _row(step_id="story-design", from_state="awaiting_result", to_state="completed",
             ts=400.0, result_status="completed", reason="ok", payload={}),
        _row(step_id="implement", from_state="pending", to_state="running",
             ts=400.0),
        _row(step_id="implement", from_state="running", to_state="awaiting_result",
             ts=401.0),
        _row(step_id="implement", from_state="awaiting_result", to_state="completed",
             ts=900.0, result_status="completed", reason="ok", payload={}),
        _row(step_id="code-review", from_state="pending", to_state="running",
             ts=900.0),
        _row(step_id="code-review", from_state="running", to_state="awaiting_result",
             ts=901.0),
        _row(step_id="code-review", from_state="awaiting_result", to_state="failed",
             ts=1000.0, result_status="changes_requested", reason="ok", payload=None),
        # The engine refuses implement visit 2 (cap) while code-review is
        # still the open visit.
        _row(step_id="implement", from_state="running", to_state="failed",
             ts=1001.0, result_status=None,
             reason="step 'implement' visited 4 times in one dispatch (cap 3) — runaway loop halted, escalating for a human",
             payload={}),
    ]
    nodes = _build_timeline(rows, WORKFLOW, GATES, run_state="failed")["nodes"]
    assert [(n["step_id"], n["visit_no"]) for n in nodes] == [
        ("story-design", 1), ("implement", 1), ("code-review", 1),
        ("implement", 2), ("pr-create", 0),
    ]
    halt = nodes[3]
    assert halt["state"] == "failed"
    assert halt["verdict"] is None
    assert "runaway loop halted" in halt["reason"]
    # The closed code-review visit keeps its verdict.
    assert nodes[2]["verdict"] == "changes_requested"
