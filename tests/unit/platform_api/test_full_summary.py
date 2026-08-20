"""Unit — the on-demand full-summary selection logic.

Poll payloads carry only the 1500-char summary head; the complete text is
stored on the completion transition's payload as ``summary_full`` and fetched
by ``GET /api/runs/{id}/summary``. The picker is pure over a newest-first
payload list so it unit-tests without a database.
"""

from types import SimpleNamespace

from platform_api.routers.runs import (
    _latest_step_payload,
    _pick_full_summary,
    _summary_payloads,
)


def test_pick_commit_pinned_visit_returns_its_full_text():
    # Multi-visit loops store a different sha per visit — the pin selects
    # THIS visit's text, not the newest visit's.
    payloads = [
        {"summary": "visit2 head", "summary_full": "visit2 full", "commit": "def"},
        {"summary": "visit1 head", "summary_full": "visit1 full", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, "abc") == ("visit1 full", "abc", False)


def test_pick_commit_pinned_visit_without_full_falls_back_truncated():
    # Pre-feature runs stored no summary_full — the pinned visit still answers,
    # flagged so the UI knows this is all there is.
    payloads = [{"summary": "visit1 head", "commit": "abc"}]
    assert _pick_full_summary(payloads, "abc") == ("visit1 head", "abc", True)


def test_pick_unknown_commit_is_ignored_and_newest_full_wins():
    payloads = [
        {"summary": "new head", "summary_full": "new full", "commit": "def"},
        {"summary": "old head", "summary_full": "old full", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, "zzz9999") == ("new full", "def", False)


def test_pick_no_commit_newest_full_wins():
    payloads = [
        {"summary": "new head", "summary_full": "new full", "commit": "def"},
        {"summary": "old head", "summary_full": "old full", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, None) == ("new full", "def", False)


def test_pick_full_missing_everywhere_returns_newest_head_truncated():
    payloads = [
        {"summary": "later head", "commit": "def"},
        {"summary": "earlier head", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, None) == ("later head", "def", True)


def test_pick_empty_full_string_treated_as_absent():
    payloads = [{"summary": "head", "summary_full": "", "commit": "abc"}]
    assert _pick_full_summary(payloads, None) == ("head", "abc", True)


def test_pick_no_payloads_returns_none():
    assert _pick_full_summary([], None) == (None, None, True)


def test_pick_commit_pinned_prefers_full_on_older_row_over_gate_head():
    # The gate card (newest) can carry the same commit as the completion row
    # behind it — the pin must keep scanning for the full text instead of
    # answering with the gate card's truncated head.
    payloads = [
        {"summary": "head", "result_status": "escalation_required",
         "commit": "abc"},                              # gate card
        {"summary": "head", "summary_full": "full", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, "abc") == ("full", "abc", False)


def test_pick_gate_card_first_full_on_older_completion_row():
    # While paused, the newest content row is the gate card (truncated head,
    # no summary_full — it stays poll-light); the completion row just behind
    # it carries the full text.
    payloads = [
        {"summary": "head", "files": [], "result_status": "completed"},  # gate card
        {"summary": "head", "summary_full": "head plus the rest", "commit": "abc"},
    ]
    assert _pick_full_summary(payloads, None) == ("head plus the rest", "abc", False)


def _fake_db(rows):
    async def execute(stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
    return SimpleNamespace(execute=execute)


async def test_summary_payloads_skips_contentless_rows():
    rows = [
        SimpleNamespace(payload={"summary": "head", "summary_full": "full",
                                 "commit": "abc"}),
        SimpleNamespace(payload={}),  # empty approval record
        SimpleNamespace(payload={"files": [{"path": "a.py"}]}),  # no summary keys
        SimpleNamespace(payload=None),
    ]
    out = await _summary_payloads(_fake_db(rows), "run-1", "implement")
    assert out == [{"summary": "head", "summary_full": "full", "commit": "abc"}]


async def test_summary_payloads_query_has_no_to_state_filter():
    # Regression (run 07c4b440): the engine records non-happy verdict rows
    # (escalation_required / BLOCK / changes_requested) with to_state="failed"
    # — a to_state filter here would 404 their full summaries.
    captured = {}

    async def execute(stmt):
        captured["stmt"] = stmt
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

    db = SimpleNamespace(execute=execute)
    assert await _summary_payloads(db, "run-1", "story-design") == []
    # to_state legitimately appears as a SELECTED column — assert only on the
    # WHERE portion of the compiled query.
    where_clause = str(captured["stmt"].compile()).split("WHERE")[-1]
    assert "to_state" not in where_clause


async def test_latest_step_payload_non_happy_verdict_row_is_served():
    # The engine records changes_requested / BLOCK / escalation rows with
    # to_state="failed" — their payload IS the display payload for the visit.
    rows = [
        SimpleNamespace(payload={"commit": "105c655",
                                 "review_files": [{"path": "code-review.md"}],
                                 "summary": "review done"}),
    ]
    out = await _latest_step_payload(_fake_db(rows), "run-1", "code-review")
    assert out["commit"] == "105c655"


async def test_latest_step_payload_query_has_no_to_state_filter():
    # Same trap family as _summary_payloads / resolve_step_sha (run cafbe28c):
    # a to_state filter would drop non-happy verdict rows and fall back to
    # the demo stubs on the run-detail page.
    captured = {}

    async def execute(stmt):
        captured["stmt"] = stmt
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=list))

    db = SimpleNamespace(execute=execute)
    assert await _latest_step_payload(db, "run-1", "code-review") == {}
    where_clause = str(captured["stmt"].compile()).split("WHERE")[-1]
    assert "to_state" not in where_clause
