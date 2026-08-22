"""Unit tests — context injection purity (port of R&D test t6)."""

import json

from engine_service.contexts import STATUS_MEANINGS, build_step_context
from engine_service.workflow import PolicySpec, WorkflowSpec

WF_YAML = """
workflow: story-delivery
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: test-creator
      changes_requested: story-design
  - id: test-creator
    skill: test-creator
    model: low
    "on":
      completed: DONE
"""

POLICY_YAML = """
policy: strict
gates:
  story-design: {review: required, role: any}
"""


def build(step_id, *, handoff=None, reviewer_feedback="", user_query=""):
    wf = WorkflowSpec.load_yaml(WF_YAML)
    pol = PolicySpec.load_yaml(POLICY_YAML)
    return build_step_context("r1", step_id, step_id, "STORY-1", wf, pol,
                              reviewer_feedback=reviewer_feedback, handoff=handoff,
                              user_query=user_query)


def test_context_never_leaks_routing_targets():
    """t6 port: the context is the skill's vocabulary and audience — it must not name
    where any verdict routes, or the skill could game the workflow."""
    sd = build("story-design")
    serialized = json.dumps(sd)
    assert "test-creator" not in serialized, "routing target leaked into context!"
    assert "DONE" not in serialized, "routing target leaked into context!"


def test_gate_follows_matches_policy():
    sd = build("story-design")
    tc = build("test-creator")
    assert sd["gate_follows"] is True
    assert sd["gate_role"] == "any"
    assert "review" in sd["advice"]
    assert tc["gate_follows"] is False
    assert tc["gate_role"] is None
    assert "no human" in tc["advice"]


def test_status_meanings_filtered_to_vocabulary():
    """The skill sees meanings for exactly the statuses the workflow can route."""
    sd = build("story-design")
    allowed = set(sd["allowed_result_statuses"])
    assert set(sd["result_status_meanings"]) == {k for k in allowed if k in STATUS_MEANINGS}
    assert "changes_requested" in allowed              # routable → vocabulary included
    assert "BLOCK" not in sd["result_status_meanings"]  # unroutable → not offered


def test_handoff_self_loop_guard():
    """A step re-running after its own changes_requested must not get its own verdict
    fed back as an upstream handoff."""
    handoff = {"from_step": "story-design", "status": "changes_requested",
               "summary": "revise", "report_files": ["review.md"]}
    sd = build("story-design", handoff=handoff)
    assert sd["upstream_handoff"] is None


def test_handoff_visible_to_next_step():
    handoff = {"from_step": "story-design", "status": "changes_requested",
               "summary": "revise", "report_files": ["review.md"]}
    tc = build("test-creator", handoff=handoff)
    assert tc["upstream_handoff"] == handoff


def test_reviewer_feedback_passed_through():
    sd = build("story-design", reviewer_feedback="please tighten the acceptance criteria")
    assert sd["reviewer_feedback"] == "please tighten the acceptance criteria"
    assert build("test-creator")["reviewer_feedback"] == ""


def test_user_query_carried_verbatim():
    """ADR-016: the ad-hoc query rides the BB_CONTEXT channel unchanged — the
    runner turns it into the prompt."""
    query = "fix the flaky login test and run the full suite"
    sd = build("story-design", user_query=query)
    assert sd["user_query"] == query


def test_user_query_defaults_to_empty_for_pipeline_steps():
    """Pipeline steps never carry a query — empty string, not None, so the
    runner's jq extraction (`.user_query // ""`) behaves identically."""
    assert build("story-design")["user_query"] == ""
    assert json.dumps(build("story-design")) is not None
