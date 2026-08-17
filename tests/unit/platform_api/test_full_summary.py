"""Unit — the on-demand full-summary selection logic.

Poll payloads carry only the 1500-char summary head; the complete text is
stored on the completion transition's payload as ``summary_full`` and fetched
by ``GET /api/runs/{id}/summary``. The picker is pure over a newest-first
payload list so it unit-tests without a database.
"""

from types import SimpleNamespace

from platform_api.routers.runs import _pick_full_summary, _summary_payloads


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
