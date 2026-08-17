"""Unit — the single log-key builder shared by engine uploads and platform reads."""

import pytest

from bheembhai.log_keys import KINDS, log_key


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
