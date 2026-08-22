"""Unit tests — workflow/policy YAML parsing, validation, and tier resolution."""

from pathlib import Path

import pytest

from engine_service.workflow import (
    PairingError,
    PolicySpec,
    TierResolutionError,
    WorkflowError,
    WorkflowSpec,
    allowed_models,
    resolve_model_tier,
    validate_pairing,
    validate_workflow,
)
from platform_api.routers._workflow_shared import _parse_workflow_yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

WF_YAML = """
workflow: story-delivery
version: 1
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: test-creator
      escalation_required: tech-design
  - id: tech-design
    skill: tech-design
    model: high
    "on":
      completed: story-design
  - id: test-creator
    skill: test-creator
    model: low
    "on":
      completed: implement
  - id: implement
    skill: implement
    model: medium
    "on":
      completed: DONE
"""

POLICY_YAML = """
policy: strict
version: 1
applies_to: story-delivery
gates:
  story-design: {review: required, role: any}
  implement: {review: required, role: lead}
"""


def load_wf(yaml_str=WF_YAML):
    return WorkflowSpec.load_yaml(yaml_str)


def test_load_yaml_parses_steps_in_order():
    wf = load_wf()
    assert wf.name == "story-delivery"
    assert wf.start == "story-design"
    assert list(wf.steps) == ["story-design", "tech-design", "test-creator", "implement"]


def test_load_yaml_fixes_bare_on_key():
    """YAML 1.1 parses a bare `on:` key as boolean True — the loader must move it."""
    wf = load_wf("""
workflow: w
start: a
steps:
  - id: a
    skill: s
    on: DONE
""")
    assert wf.steps["a"]["on"] == {"completed": "DONE"}
    assert True not in wf.steps["a"]
    assert wf.allowed_statuses("a") == ["completed"]
    assert wf.route_for("a", "completed") == "DONE"


def test_allowed_statuses_includes_completed_always():
    wf = load_wf()
    assert "completed" in wf.allowed_statuses("story-design")
    assert "escalation_required" in wf.allowed_statuses("story-design")
    assert "BLOCK" not in wf.allowed_statuses("story-design")
    # a step with no on: map can still always succeed
    assert wf.allowed_statuses("implement") == ["completed"]


def test_route_for_and_gate_for():
    wf = load_wf()
    policy = PolicySpec.load_yaml(POLICY_YAML)
    assert wf.route_for("story-design", "completed") == "test-creator"
    assert wf.route_for("story-design", "BLOCK") is None
    assert policy.gate_for("story-design", "completed")["role"] == "any"
    assert policy.gate_for("implement", "completed")["role"] == "lead"
    assert policy.gate_for("story-design", "BLOCK") is None    # on_status defaults to completed
    assert policy.gate_for("test-creator", "completed") is None


def test_gate_for_respects_on_status():
    policy = PolicySpec.load_yaml("""
policy: governed
gates:
  implement: {review: required, on_status: [completed, BLOCK]}
""")
    assert policy.gate_for("implement", "completed") is not None
    assert policy.gate_for("implement", "BLOCK") is not None
    assert policy.gate_for("implement", "changes_requested") is None


def test_validate_workflow_happy_path():
    assert validate_workflow(load_wf(), known_skills={"story-design", "tech-design",
                                                      "test-creator", "implement"}) is True


def test_validate_workflow_rejects_unknown_route_target():
    bad = WF_YAML.replace("completed: test-creator", "completed: mystery-step")
    with pytest.raises(WorkflowError, match="mystery-step"):
        validate_workflow(load_wf(bad))


def test_validate_workflow_rejects_undefined_start():
    bad = WF_YAML.replace("start: story-design", "start: nope")
    with pytest.raises(WorkflowError, match="start step"):
        validate_workflow(load_wf(bad))


def test_validate_workflow_rejects_stale_concrete_model_id():
    """ADR-013 tier migration: concrete model ids are no longer workflow vocabulary."""
    bad = WF_YAML.replace("model: high", "model: claude-opus-4-8")
    with pytest.raises(WorkflowError, match="not a model tier"):
        validate_workflow(load_wf(bad))


def test_validate_workflow_rejects_unknown_skill():
    with pytest.raises(WorkflowError, match="not installed"):
        validate_workflow(load_wf(), known_skills={"story-design"})


def test_validate_pairing_happy_path():
    assert validate_pairing(load_wf(), PolicySpec.load_yaml(POLICY_YAML)) is True


def test_validate_pairing_rejects_gate_on_unknown_step():
    bad_policy = PolicySpec.load_yaml("""
policy: p
gates:
  ghost-step: {review: required}
""")
    with pytest.raises(PairingError, match="ghost-step"):
        validate_pairing(load_wf(), bad_policy)


def test_validate_pairing_rejects_unroutable_on_status():
    """A human gate on a status the workflow can't route would approve into a dead end."""
    bad_policy = PolicySpec.load_yaml("""
policy: p
gates:
  story-design: {review: required, on_status: [completed, BLOCK]}
""")
    with pytest.raises(PairingError, match="BLOCK"):
        validate_pairing(load_wf(), bad_policy)


VENDOR_CONFIG = {
    "base_url": "https://api.deepseek.example",
    "model_high": "deepseek-v4-pro",
    "model_medium": "deepseek-v4-mid",
    "model_low": "deepseek-v4-flash",
}


def test_resolve_model_tier_maps_through_integration_config():
    assert resolve_model_tier("high", VENDOR_CONFIG) == "deepseek-v4-pro"
    assert resolve_model_tier("medium", VENDOR_CONFIG) == "deepseek-v4-mid"
    assert resolve_model_tier("low", VENDOR_CONFIG) == "deepseek-v4-flash"
    assert resolve_model_tier(None, VENDOR_CONFIG) is None


def test_resolve_model_tier_missing_mapping_fails_loudly():
    with pytest.raises(TierResolutionError, match="model_medium"):
        resolve_model_tier("medium", {"model_high": "x", "model_low": "y"})
    with pytest.raises(TierResolutionError):
        resolve_model_tier("high", None)


def test_resolve_model_tier_literal_passthrough():
    """A literal vendor id (not a tier) passes through — init's BB_ALLOWED_MODELS check
    then decides whether it is allowed."""
    assert resolve_model_tier("gpt-5.2-mini", VENDOR_CONFIG) == "gpt-5.2-mini"


def test_allowed_models_is_the_three_resolved_ids():
    assert allowed_models(VENDOR_CONFIG) == [
        "deepseek-v4-pro", "deepseek-v4-mid", "deepseek-v4-flash"]
    assert allowed_models({"model_high": "only"}) == ["only"]


# ── Seeded ad-hoc workflow/policy (ADR-016) — guard the seed files themselves ──


def test_seeded_adhoc_workflow_parses_and_routes_to_done():
    """The ad-hoc template must stay a 1-step workflow that terminates — the
    engine's `_loop` already handles `DONE`, so no state-machine change."""
    yaml_text = (REPO_ROOT / "config" / "workflow-adhoc.yaml").read_text()
    wf = WorkflowSpec.load_yaml(yaml_text)
    assert wf.name == "adhoc"
    assert wf.start == "adhoc"
    assert list(wf.steps) == ["adhoc"]
    assert wf.route_for("adhoc", "completed") == "DONE"
    # The platform's 1-step guard for ad-hoc submits parses the same file.
    parsed = _parse_workflow_yaml(yaml_text)
    assert parsed is not None and len(parsed.steps) == 1
    assert parsed.steps[0].id == "adhoc"


def test_seeded_adhoc_policy_is_gate_free_and_pairs():
    """Wide-open by design: zero gates, and the workflow/policy pairing
    validates so run submit never 422s on the template itself."""
    policy = PolicySpec.load_yaml(
        (REPO_ROOT / "config" / "policy-adhoc.yaml").read_text())
    assert policy.gates == {}
    wf = WorkflowSpec.load_yaml(
        (REPO_ROOT / "config" / "workflow-adhoc.yaml").read_text())
    assert validate_pairing(wf, policy) is True
