"""Workflow/policy parsing, validation, and model-tier resolution (from DB content).

The platform stores workflow/policy definitions as YAML strings (`workflows.yaml_content`,
`policies.yaml_content`). This module parses and validates them for the engine — ported
from the R&D engine's `Workflow`/`Policy` dataclasses (engine.py), with one deliberate
change: model resolution no longer goes through a `model_profiles` indirection. ADR-013
replaces it with flat tier keys (`model_high/medium/low`) on the AI-vendor integration.
"""

from dataclasses import dataclass, field

import re
import yaml

from engine_service.runtime import ExecState, Result, TRANSIENT  # noqa: F401  (re-export)

__all__ = [
    "Result", "TRANSIENT", "ExecState",
    "WorkflowSpec", "PolicySpec",
    "WorkflowError", "PairingError", "TierResolutionError",
    "validate_workflow", "validate_pairing",
    "TIER_KEYS", "resolve_model_tier", "allowed_models",
]


class WorkflowError(ValueError):
    """A malformed or internally-inconsistent workflow — caught at load, not mid-run."""


class PairingError(ValueError):
    """A workflow and policy that don't fit together — caught at load, not at runtime."""


class TierResolutionError(ValueError):
    """A step's model tier can't be mapped through the AI-vendor integration's config."""


# ADR-013 §2: model tiers are high/medium/low everywhere. Each AI-vendor integration
# carries the tier -> concrete vendor model id mapping as flat config keys.
TIER_KEYS = {"high": "model_high", "medium": "model_medium", "low": "model_low"}


@dataclass
class WorkflowSpec:
    name: str
    start: str
    steps: dict    # step_id -> step spec (insertion order = workflow order)

    @classmethod
    def load_yaml(cls, content: str) -> "WorkflowSpec":
        d = yaml.safe_load(content)
        steps = {}
        for s in d["steps"]:
            if True in s and "on" not in s:   # YAML 1.1 reads bare `on:` as boolean True
                s["on"] = s.pop(True)
            if isinstance(s.get("on"), str):  # `on: DONE` — success routes straight to DONE
                s["on"] = {Result.COMPLETED: s["on"]}
            steps[s["id"]] = s
        return cls(d["workflow"], d["start"], steps)

    def allowed_statuses(self, step_id: str) -> list[str]:
        """The result statuses this step may emit that the workflow knows how to route.

        This is the skill's VALID VOCABULARY for this run — the keys of its `on:` block.
        Deliberately returns the status NAMES only, never their targets: the skill learns
        what it may say, not where each choice leads. `completed` is always allowed (a step
        can always succeed); failure statuses are engine-level and not skill-selectable here.
        """
        spec = self.steps.get(step_id, {})
        keys = set((spec.get("on") or {}).keys())
        keys.add(Result.COMPLETED)
        return sorted(keys)

    def route_for(self, step_id: str, status: str) -> str | None:
        """Where `status` routes from this step — None means 'no route' (engine halts)."""
        return (self.steps.get(step_id, {}).get("on") or {}).get(status)


@dataclass
class PolicySpec:
    name: str
    gates: dict = field(default_factory=dict)    # step_id -> gate spec

    @classmethod
    def load_yaml(cls, content: str) -> "PolicySpec":
        d = yaml.safe_load(content)
        return cls(d["policy"], d.get("gates") or {})

    def gate_for(self, step_id: str, status: str) -> dict | None:
        """The gate that applies to this step for this outcome, or None.

        A gate may declare `on_status: [...]` to require a human on non-happy outcomes
        (BLOCK, changes_requested, escalation_required) as well as on success. Defaults to
        ["completed"], so existing policies behave exactly as before.

        Note the boundary: policy decides WHETHER a human is consulted; the workflow still
        decides WHERE control goes afterwards.
        """
        gate = self.gates.get(step_id)
        if not gate:
            return None
        applies = gate.get("on_status") or [Result.COMPLETED]
        return gate if status in applies else None


def resolve_model_tier(tier: str | None, ai_vendor_config: dict | None) -> str | None:
    """Map a workflow step's model tier through the AI-vendor integration's flat config
    keys (ADR-013 §2 step 5) to a concrete vendor model id.

    `high`/`medium`/`low` resolve via `model_high`/`model_medium`/`model_low`. A literal
    value (a workflow that hardcodes a vendor model id) passes through unchanged. A tier
    with no mapping raises TierResolutionError — init fails with a clear error before any
    container launches.
    """
    if not tier:
        return None
    key = TIER_KEYS.get(tier)
    if key is None:
        return tier    # literal model id — pass through as-is
    value = (ai_vendor_config or {}).get(key, "")
    if not value:
        raise TierResolutionError(
            f"AI-vendor integration has no '{key}' config key to map tier '{tier}'")
    return value


def allowed_models(ai_vendor_config: dict | None) -> list[str]:
    """BB_ALLOWED_MODELS for a run = the integration's three resolved tier mappings."""
    config = ai_vendor_config or {}
    return [config[k] for k in TIER_KEYS.values() if config.get(k)]


def validate_workflow(workflow: WorkflowSpec, known_skills: set[str] | None = None) -> bool:
    """Reject a malformed workflow before any container launches.

    Catches, at load time, the classes of error that would otherwise surface mid-run after
    money has been spent: a routing target that doesn't exist, a start step that isn't
    defined, a step missing its skill, an unknown model tier. All cheap to check, all far
    better as an instant clear error than a container launch followed by a halt.
    """
    problems = []
    steps = workflow.steps or {}

    # structure: start resolves, every step has id + skill
    if not workflow.start or workflow.start not in steps:
        problems.append(f"start step '{workflow.start}' is not defined")
    for sid, spec in steps.items():
        if not spec.get("skill"):
            problems.append(f"step '{sid}' has no skill")
        # Step ids become directory names (attempt dirs) and object-store keys
        # (logs/<run>/<step>/<attempt>/…): keep them to a safe slug charset so
        # a weird id can never escape its directory or collide after
        # sanitization. log_keys._slug is the belt-and-braces fallback.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", sid):
            problems.append(
                f"step id '{sid}' is not a safe slug "
                f"(lowercase a-z, 0-9, dashes, max 64 chars)")

    # routing: every on: target resolves to a real step (or route_to / DONE)
    for sid, spec in steps.items():
        for status, target in (spec.get("on") or {}).items():
            if target in ("route_to", "DONE"):
                continue
            if target not in steps:
                problems.append(
                    f"step '{sid}' routes '{status}' -> '{target}', which is not a defined step")

    # models: tiers are the only declared vocabulary (ADR-013 §2 tier migration) — anything
    # else is a stale concrete model id or a typo, both better caught than launched.
    for sid, spec in steps.items():
        m = spec.get("model")
        if m and m not in TIER_KEYS:
            problems.append(
                f"step '{sid}' uses model '{m}', which is not a model tier "
                f"(valid: {sorted(TIER_KEYS)})")

    # skills: every skill is one the platform knows about (when a list is provided)
    if known_skills is not None:
        for sid, spec in steps.items():
            sk = spec.get("skill")
            if sk and sk not in known_skills:
                problems.append(f"step '{sid}' uses skill '{sk}', which is not installed")

    if problems:
        raise WorkflowError(
            "workflow is invalid:\n  - " + "\n  - ".join(problems))
    return True


def validate_pairing(workflow: WorkflowSpec, policy: PolicySpec) -> bool:
    """Reject a workflow+policy pairing whose gates can't be honoured.

    The rule: policy governs WHETHER a human weighs in on a transition the workflow defines.
    It cannot invent transitions. So a gate's `on_status` may only list statuses the workflow
    can actually route from that step (its `on:` keys, plus `completed`, which always exists).

    Without this check, a policy could pause for a human on, say, story-design→BLOCK when the
    workflow has no BLOCK route there — the reviewer approves, and the engine then halts with
    'no route defined', having wasted the review. Catching it here makes that impossible to
    ship rather than latent until the status happens to fire.
    """
    problems = []
    for step_id, gate in (policy.gates or {}).items():
        if step_id not in workflow.steps:
            problems.append(
                f"policy '{policy.name}' gates step '{step_id}', which workflow "
                f"'{workflow.name}' does not define")
            continue
        routable = set(workflow.allowed_statuses(step_id))   # on: keys + completed
        for st in (gate.get("on_status") or [Result.COMPLETED]):
            if st not in routable:
                problems.append(
                    f"policy '{policy.name}' gate '{step_id}' waits for review on '{st}', "
                    f"but workflow '{workflow.name}' has no route for '{st}' from '{step_id}' "
                    f"(routable: {sorted(routable)}). A human would approve into a dead end.")
    if problems:
        raise PairingError(
            "workflow/policy pairing is inconsistent:\n  - " + "\n  - ".join(problems))
    return True
