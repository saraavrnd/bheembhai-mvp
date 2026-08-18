"""Unit — referenced-skill-name extraction from parsed workflow YAML.

Clone-on-map uses these names to decide which platform skills to copy into
project scope. The fallback to the step id mirrors the engine's
``spec.get("skill", step_id)`` resolution.
"""

from platform_api.routers._workflow_shared import (
    _parse_workflow_yaml,
    _referenced_skill_names,
)
from platform_api.schemas.admin import WorkflowParsed, WorkflowStepSchema


def _parsed(steps: list[WorkflowStepSchema]) -> WorkflowParsed:
    return WorkflowParsed(
        workflow="story-delivery", version=1,
        start=steps[0].id if steps else "", steps=steps,
    )


def _step(skill: str, step_id: str = "s1") -> WorkflowStepSchema:
    return WorkflowStepSchema(id=step_id, skill=skill)


def test_extracts_unique_skill_names():
    parsed = _parsed([
        _step("story-design", "design"),
        _step("test-creator", "tests"),
        _step("story-design", "design-recheck"),   # duplicate name
    ])
    assert _referenced_skill_names(parsed) == {"story-design", "test-creator"}


def test_empty_skill_falls_back_to_step_id():
    parsed = _parsed([_step("", "implement")])
    assert _referenced_skill_names(parsed) == {"implement"}


def test_whitespace_only_skill_is_dropped():
    # Truthy-but-blank skill names strip to empty and are not referenced.
    parsed = _parsed([_step("  ", "verify")])
    assert _referenced_skill_names(parsed) == set()


def test_strips_whitespace():
    parsed = _parsed([_step("  code-review  ", "review")])
    assert _referenced_skill_names(parsed) == {"code-review"}


def test_none_parsed_returns_empty_set():
    assert _referenced_skill_names(None) == set()


def test_no_steps_returns_empty_set():
    assert _referenced_skill_names(_parsed([])) == set()


def test_real_yaml_round_trip():
    yaml_content = """
workflow: story-delivery
version: 1
start: story-design
steps:
  - id: story-design
    skill: story-design
    model: high
    "on":
      completed: test-creator
  - id: test-creator
    skill: test-creator
    model: medium
"""
    parsed = _parse_workflow_yaml(yaml_content)
    assert _referenced_skill_names(parsed) == {"story-design", "test-creator"}
