"""Pure run/visit counters for the Workflows catalog and workflow home.

Duck-typed over transition rows (id-ordered), so the counting logic is
unit-testable without a database — the same design as
``runs._build_timeline``, whose visit-opening block is the CANONICAL
implementation of visit grouping. ``_visit_stream`` below mirrors it
minimally; a differential unit test
(``tests/unit/platform_api/test_run_stats.py``) pins the two together, so a
change to ``_build_timeline``'s merge rules must be reflected here.

Counting semantics:

- ``executions`` = number of visits (one per ``pending→running`` opening).
- ``loop_backs`` = visits with ``visit_no > 1``. Re-visits cover BOTH human
  send-backs (the engine re-routes to the target step, opening a new visit)
  and workflow-routed loops (``changes_requested`` → re-route). Counting
  decision rows on top would double-count the same event.
- ``duration_s`` = last run-level terminal transition ts − first run-level
  ``pending→running`` ts (fallback: the run's ``created_at``). ``None`` for
  runs with no terminal row. ``Run`` has no ``completed_at`` column.
- ``gate_waits`` = per gated visit: decision ts − ``awaiting_approval`` ts.
"""

from collections import Counter
from datetime import datetime, timezone
from itertools import pairwise

from platform_api.routers.runs import TERMINAL_RUN_STATES, _gate_decision_from

__all__ = ["_median", "_relative_time", "_run_stats", "_visit_stream"]


def _visit_stream(rows) -> list[dict]:
    """Rebuild visits in execution order — minimal mirror of ``_build_timeline``.

    Returns one dict per visit: ``{step_id, visit_no, started_ts, ended_ts,
    gate_open_ts, gate_decision, halt, attempt_no}``. Run-level bookkeeping
    rows (``step_id`` falsy) are skipped — those feed ``_run_stats`` directly.
    """
    visits: list[dict] = []
    open_visit: dict | None = None
    by_step: dict[str, int] = {}

    for row in rows:
        if not row.step_id:
            continue  # run-level bookkeeping (init, model resolution, resets)
        if row.from_state == "pending" and row.to_state == "running":
            if (open_visit is not None and open_visit["step_id"] == row.step_id
                    and open_visit["ended_ts"] is None):
                # The SAME visit re-announced: a deadline retry or a
                # crash-recovery resume both re-fire pending→running while
                # the step never closed (ended_ts is only set by a verdict
                # or a gate). Merging keeps one visit — see _build_timeline.
                continue
            if open_visit is not None:
                visits.append(open_visit)
            visit_no = by_step.get(row.step_id, 0) + 1
            by_step[row.step_id] = visit_no
            open_visit = {
                "step_id": row.step_id, "visit_no": visit_no,
                "started_ts": float(row.ts), "ended_ts": None,
                "gate_open_ts": None, "gate_decision": None,
                "halt": False, "attempt_no": row.attempt_no or 1,
            }
            continue
        v = open_visit
        if v is None or v["step_id"] != row.step_id:
            # A failure row addressed to a step whose visit already closed —
            # e.g. the runaway-loop cap. Surface as a halted visit (mirrors
            # _build_timeline's halt nodes) so it still counts as an execution.
            if row.to_state == "failed" and row.from_state == "running":
                if open_visit is not None:
                    visits.append(open_visit)
                    open_visit = None
                visit_no = by_step.get(row.step_id, 0) + 1
                by_step[row.step_id] = visit_no
                visits.append({
                    "step_id": row.step_id, "visit_no": visit_no,
                    "started_ts": None, "ended_ts": None,
                    "gate_open_ts": None, "gate_decision": None,
                    "halt": True, "attempt_no": row.attempt_no or 1,
                })
            continue
        if row.to_state == "awaiting_approval":
            # The gate card is the visit's effective end; the wait surfaces
            # as the decision edge.
            v["gate_open_ts"] = float(row.ts)
            v["ended_ts"] = float(row.ts)
            continue
        if row.from_state == "awaiting_approval" and row.to_state == "completed":
            decision = _gate_decision_from(row)
            if decision:
                v["gate_decision"] = decision
            continue
        if (row.to_state in ("completed", "failed")
                and row.result_status and v["ended_ts"] is None):
            # First terminal row carrying a verdict closes the visit.
            v["ended_ts"] = float(row.ts)
    if open_visit is not None:
        visits.append(open_visit)

    return visits


def _run_stats(rows, created_ts: float) -> dict:
    """Per-run counters: ``{executions, loop_backs, loop_edges, duration_s,
    gate_waits}``.

    ``loop_edges`` is a ``Counter[(from_step, to_step)]`` over consecutive
    visits where the second is a re-visit (``visit_no > 1``) — the loop-back
    edges, used for "most common loop-back".
    """
    visits = _visit_stream(rows)

    loop_backs = sum(1 for v in visits if v["visit_no"] > 1)
    loop_edges: Counter = Counter()
    for prev, cur in pairwise(visits):
        if cur["visit_no"] > 1:
            loop_edges[(prev["step_id"], cur["step_id"])] += 1

    # Run-level rows: init is the first pending→running, the terminal row is
    # the last one landing in a terminal state (engine writes these rows).
    init_ts: float | None = None
    terminal_ts: float | None = None
    for row in rows:
        if row.step_id:
            continue
        if init_ts is None and row.from_state == "pending" and row.to_state == "running":
            init_ts = float(row.ts)
        if row.to_state in TERMINAL_RUN_STATES:
            terminal_ts = float(row.ts)

    duration_s: float | None = None
    if terminal_ts is not None:
        duration_s = terminal_ts - (init_ts if init_ts is not None else created_ts)

    gate_waits: list[float] = [
        v["gate_decision"]["ts"] - v["gate_open_ts"]
        for v in visits
        if v["gate_open_ts"] is not None and v["gate_decision"] is not None
    ]

    return {
        "executions": len(visits),
        "loop_backs": loop_backs,
        "loop_edges": loop_edges,
        "duration_s": duration_s,
        "gate_waits": gate_waits,
    }


def _median(values: list[float]) -> float | None:
    """Median of a list of floats; None when empty."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float((s[mid - 1] + s[mid]) / 2)


def _relative_time(ts: float | None) -> str | None:
    """Human-readable relative time from a unix ts (e.g. "3m ago")."""
    if ts is None:
        return None
    now = datetime.now(timezone.utc).timestamp()
    mins = int(max(0, now - ts) // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    return f"{hrs // 24}d ago"
