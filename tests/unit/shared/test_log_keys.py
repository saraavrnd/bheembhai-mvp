"""Unit — the single key builder shared by engine uploads and platform reads."""

import pytest
from bheembhai.log_keys import (
    KINDS,
    PROGRESS_FILENAME,
    RESULT_FILENAME,
    log_key,
    progress_key,
    result_key,
    session_transcript_key,
    turn_inbox_key,
    turn_outbox_key,
)


def test_result_key_is_canonical():
    assert result_key("r1", "story-design", 2) == \
        "results/r1/story-design/2/bb_step_result.json"
    assert RESULT_FILENAME == "bb_step_result.json"


def test_progress_key_is_canonical():
    assert progress_key("r1", "story-design", 2) == \
        "results/r1/story-design/2/progress.json"
    assert PROGRESS_FILENAME == "progress.json"


def test_result_keys_share_the_slug_rules():
    """The results/ namespace reuses the same slug sanitizer as logs/."""
    assert result_key("r1", "Story Design!", 1) == \
        "results/r1/story-design/1/bb_step_result.json"
    assert progress_key("r1", "../../etc/passwd", 1) == \
        "results/r1/etc-passwd/1/progress.json"


def test_log_key_is_canonical():
    assert log_key("r1", "story-design", 2, "agent") == \
        "logs/r1/story-design/2/agent.log"
    assert log_key("r1", "story-design", 2, "container") == \
        "logs/r1/story-design/2/container.log"
    assert log_key("r1", "story-design", 2, "diagnostics") == \
        "logs/r1/story-design/2/diagnostics.txt"


def test_log_key_slug_folds_legacy_step_ids():
    """Belt-and-braces sanitizer: workflow validation already rejects these
    ids, but a legacy/odd id must never escape the attempt dir in the key."""
    assert log_key("r1", "Story Design!", 1, "agent") == \
        "logs/r1/story-design/1/agent.log"
    assert log_key("r1", "story  design", 1, "agent") == \
        "logs/r1/story-design/1/agent.log"
    assert log_key("r1", "story.design", 1, "agent") == \
        "logs/r1/story-design/1/agent.log"
    # Path separators and dot-dots collapse — never a traversal vector.
    assert log_key("r1", "../../etc/passwd", 1, "agent") == \
        "logs/r1/etc-passwd/1/agent.log"


def test_log_key_slug_empty_falls_back_to_step():
    assert log_key("r1", "", 1, "agent") == "logs/r1/step/1/agent.log"
    assert log_key("r1", "!!!", 1, "agent") == "logs/r1/step/1/agent.log"
    assert log_key("r1", "..", 1, "agent") == "logs/r1/step/1/agent.log"


def test_log_key_slug_truncates_to_64():
    key = log_key("r1", "s" * 80, 1, "agent")
    assert key == "logs/r1/" + "s" * 64 + "/1/agent.log"


def test_log_key_truncation_strips_trailing_dash():
    key = log_key("r1", "s" * 63 + "!", 1, "agent")
    assert key == "logs/r1/" + "s" * 63 + "/1/agent.log"


def test_log_key_rejects_unknown_kind():
    with pytest.raises(ValueError):
        log_key("r1", "design", 1, "video")


def test_kinds_cover_canonical_files():
    assert KINDS == ("agent", "container", "diagnostics")


# ── Session turn channels (ADR-016) ─────────────────────────────────────

def test_turn_inbox_key_is_canonical():
    assert turn_inbox_key("r1", "adhoc", 1) == "turns/r1/adhoc/1/inbox.json"


def test_turn_outbox_key_is_canonical():
    assert turn_outbox_key("r1", "adhoc", 1) == "turns/r1/adhoc/1/outbox.json"


def test_turn_keys_share_the_slug_rules():
    """The turns/ namespace uses the same slug sanitizer — a rebuilt Handle
    derives the same keys, and odd ids can never escape the attempt dir."""
    assert turn_inbox_key("r1", "../../etc/passwd", 2) == \
        "turns/r1/etc-passwd/2/inbox.json"
    assert turn_outbox_key("r1", "", 1) == "turns/r1/step/1/outbox.json"


def test_turn_keys_are_attempt_scoped_not_turn_scoped():
    """One stable key per container incarnation: the engine overwrites the
    inbox per turn and the outbox matches on `seq` inside the payload —
    turn number never appears in the key."""
    assert turn_inbox_key("r1", "adhoc", 3) == turn_inbox_key("r1", "adhoc", 3)
    assert "seq" not in turn_inbox_key("r1", "adhoc", 3)


def test_session_transcript_key_is_session_scoped():
    """Session-scoped, NOT attempt-scoped: the transcript must survive
    incarnations so a cold-start container can restore it and --resume
    (ADR-016 §3). One engine-minted id per run — the run id alone scopes it,
    and the filename mirrors the CLI's on-disk name (<session-id>.jsonl)."""
    assert session_transcript_key("r1", "11111111-2222-3333-4444-555555555555") == \
        "transcripts/r1/11111111-2222-3333-4444-555555555555.jsonl"
    assert session_transcript_key("r1", "abc-123").endswith("/abc-123.jsonl")
