"""Unit tests — the state machine's pure routing helpers (no DB, no runtime)."""

from engine_service.state_machine import _gate_card, route_next, steps_after
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
