"""Unit tests — the state machine's pure routing helpers (no DB, no runtime)."""

from types import SimpleNamespace

from bheembhai.log_keys import log_key, progress_key, result_key
from bheembhai.providers.local_storage import LocalStorage

from engine_service.state_machine import (
    _clear_attempt_channels,
    _env_int,
    _gate_card,
    route_next,
    steps_after,
)
from engine_service.workflow import WorkflowSpec

WF_YAML = """
workflow: wf
start: a
steps:
  - id: a
    skill: a
    model: high
    "on":
      completed: b
      BLOCK: route_to
  - id: b
    skill: b
    model: low
    "on":
      completed: c
  - id: c
    skill: c
    model: low
    "on":
      completed: DONE
"""


def spec():
    return WorkflowSpec.load_yaml(WF_YAML)


def test_route_next_uses_on_map_by_default():
    assert route_next(spec(), "a", "completed", {}) == "b"


def test_route_next_honours_hint_only_under_route_to():
    wf = spec()
    # BLOCK routes via route_to — the skill's hint IS the target
    assert route_next(wf, "a", "BLOCK", {"next_hint": "c"}) == "c"
    # completed routes via the map — the hint is advisory and ignored
    assert route_next(wf, "a", "completed", {"next_hint": "c"}) == "b"


def test_route_next_no_route_returns_none():
    assert route_next(spec(), "b", "BLOCK", {}) is None


def test_steps_after_returns_later_steps_in_order():
    assert steps_after(spec(), "a") == ["b", "c"]
    assert steps_after(spec(), "b") == ["c"]
    assert steps_after(spec(), "c") == []


def test_steps_after_unknown_target_is_empty():
    assert steps_after(spec(), "nope") == []


def test_gate_card_carries_cost_fields():
    """The reviewer card persists per-visit spend so the UI can show what the
    gated visit cost — and whether that figure is confident or scraped."""
    card = _gate_card({"role": "reviewer"}, {
        "status": "BLOCK", "summary": "not green", "cost_usd": 0.5,
        "cost_reported": True, "cost_partial": False})
    assert card["cost_usd"] == 0.5
    assert card["cost_reported"] is True
    assert card["cost_partial"] is False


def test_gate_card_cost_defaults_when_outcome_has_no_cost():
    """Pre-cost outcomes (or a lost log) must not crash the card — the fields
    default to absent/False, which the UI reads as 'unknown'."""
    card = _gate_card({"role": "reviewer"}, {"status": "BLOCK"})
    assert card["cost_usd"] is None
    assert card["cost_reported"] is False
    assert card["cost_partial"] is False


# ── Launch hygiene: clearing stale attempt channels (ADR-014) ──────────

async def test_clear_attempt_channels_deletes_all_five_keys(tmp_path):
    store = LocalStorage(base_path=str(tmp_path / "store"))
    keys = (
        result_key("r1", "story-design", 1),
        progress_key("r1", "story-design", 1),
        log_key("r1", "story-design", 1, "agent"),
        log_key("r1", "story-design", 1, "diagnostics"),
        log_key("r1", "story-design", 1, "container"),
    )
    for key in keys:
        await store.put(key, b"stale visit-1 artifact")
    await _clear_attempt_channels(store, "r1", "story-design", 1)
    for key in keys:
        assert await store.get(key) is None


async def test_clear_attempt_channels_without_store_is_noop():
    await _clear_attempt_channels(None, "r1", "story-design", 1)   # must not raise


async def test_clear_attempt_channels_survives_delete_failure(tmp_path):
    """Best-effort hygiene — a failing backend logs and continues, it must
    never turn a launch into a crash."""
    class FlakyStore:
        backend_name = "flaky"

        async def delete(self, key):
            raise OSError("boom")

    await _clear_attempt_channels(FlakyStore(), "r1", "story-design", 1)  # no raise


# ── _env_int: guardrail knob reads from the run's resolved env vars ─────────

def _ctx(env_vars=None):
    return SimpleNamespace(env_vars=env_vars or {})


def test_env_int_uses_resolved_value():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "1"}), "BB_MAX_STEP_VISITS", 3) == 1
    assert _env_int(_ctx({"BB_MAX_ATTEMPTS": "7"}), "BB_MAX_ATTEMPTS", 2) == 7


def test_env_int_defaults_when_absent():
    assert _env_int(_ctx(), "BB_MAX_STEP_VISITS", 3) == 3


def test_env_int_clamps_to_positive():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "0"}), "BB_MAX_STEP_VISITS", 3) == 1
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "-4"}), "BB_MAX_STEP_VISITS", 3) == 1


def test_env_int_falls_back_on_garbage():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "lots"}), "BB_MAX_STEP_VISITS", 3) == 3
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": ""}), "BB_MAX_STEP_VISITS", 3) == 3
