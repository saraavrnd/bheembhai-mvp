"""Per-step context injection — the skill's vocabulary and audience, never its routing.

The backend tells the skill its valid result-status vocabulary and whether a human gate
follows — so the skill emits only routable statuses and can write its summary for a
reviewer. It does NOT include routing targets: the skill learns what it may SAY and who's
LISTENING, never where its words route the run. (Backend-authoritative routing — the
workflow's `on:` map is the only authority; see test t6 in the R&D test_engine.py.)

`build_env_bundle` composes the ADR-013 §5 container env from the run-init context —
secrets are resolved fresh per launch and never persist past this dict.
"""

import json
from typing import TYPE_CHECKING

from engine_service.runtime import Result

if TYPE_CHECKING:
    from engine_service.run_init import InitContext

# What each outcome word MEANS. The orchestrator owns the vocabulary and defines it
# structurally; the SKILL decides which one applies in its own domain. Note these
# describe the MEANING of each verdict, never where it routes — routing stays with the
# workflow so skills remain workflow-agnostic.
STATUS_MEANINGS = {
    Result.COMPLETED:
        "Your work finished successfully and the artifacts you produced are ready "
        "for whatever comes next.",
    Result.BLOCK:
        "A hard quality gate failed. The work cannot honestly proceed until "
        "something upstream changes. This is a principled stop, not 'I had "
        "trouble' — use it when proceeding would mean pretending a real problem "
        "isn't there.",
    Result.CHANGES_REQUESTED:
        "The work was reviewed and needs revision: the reviewer found defects "
        "the next pass should fix in-loop. Advisory-only findings do NOT "
        "warrant this — report those under 'completed'.",
    Result.ESCALATION_REQUIRED:
        "You hit something outside your authority: a conflict with the "
        "architecture, a missing decision, or an ambiguity only a human or an "
        "earlier stage can resolve.",
    Result.FAILED_EXECUTION:
        "You genuinely could not run to completion — bad inputs, or an error you "
        "cannot recover from.",
}


def build_step_context(run_id: str, step_id: str, skill: str, story_id: str | None,
                       workflow_spec, policy_spec, *,
                       reviewer_feedback: str = "",
                       handoff: dict | None = None) -> dict:
    """Compose the context injected into a step container. Pure — no DB, no routing
    targets. `handoff` is the prior step's non-happy verdict report (if any)."""
    allowed = workflow_spec.allowed_statuses(step_id)
    gates = policy_spec.gates
    gated = step_id in gates
    return {
        "run_id": run_id, "step_id": step_id, "skill": skill,
        "story_id": story_id,
        "reviewer_feedback": reviewer_feedback or "",
        # If this step was reached by another step's non-happy verdict, tell it why
        # and where the detail lives (e.g. test-verify's verification.md on a BLOCK).
        # Never hand a step its own verdict back (self-loop guard from the R&D engine).
        "upstream_handoff": (handoff if handoff and handoff.get("from_step") != step_id
                             else None),
        "allowed_result_statuses": allowed,
        "result_status_meanings": {k: v for k, v in STATUS_MEANINGS.items()
                                   if k in allowed},
        "gate_follows": gated,
        "gate_role": gates.get(step_id, {}).get("role"),
        "advice": ("A human will review this step's output — write `summary` for that "
                   "reviewer." if gated else
                   "This step's output routes automatically; no human will read `summary` "
                   "before the next step."),
    }


def build_env_bundle(ctx: "InitContext", *, step_id: str, attempt_no: int,
                     skill: str, model: str | None, context: dict) -> dict[str, str]:
    """ADR-013 §5 — the fully composed container env for one launch.

    Groups: Engine (identity/control-plane), Git (the run branch + credentials),
    Model (vendor key + allowed set), Jira (MCP config, optional at MVP). Every
    credential comes from the ResolvedIntegration objects on the init context —
    resolved fresh at init, never persisted in the DB or in transitions.

    Vendor key rule (Claude Code auth): the Anthropic family authenticates with
    ANTHROPIC_API_KEY; every other vendor (openai/deepseek/kimi) speaks through
    ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL.
    """
    vendor = ctx.ai_vendor
    env: dict[str, str] = {
        # Engine group
        "RUN_ID": str(ctx.run.id),
        "STEP_ID": step_id,
        "ATTEMPT_NO": str(attempt_no),
        "SKILL": skill,
        # Image-owned container-local staging dir (ADR-014: zero mounts). The
        # per-attempt PUT URLs are launch-time presigns added in state_machine,
        # not part of this bundle — same pattern as BB_SKILL_URL.
        "RESULT_DIR": "/out",
        "STORY_ID": ctx.run.story_id or "",
        # Git group
        "BB_GIT_MODE": "1",
        "GIT_REMOTE_URL": ctx.git_target.clone_url,
        "GIT_SOURCE_BRANCH": ctx.source_branch,
        "RUN_BRANCH": ctx.run_branch,
        "GH_TOKEN": ctx.github.token,
        # Model group
        "BB_MODEL": model or "",
        "BB_ALLOWED_MODELS": ",".join(_allowed_models(vendor)),
    }

    if vendor.type == "claude":
        env["ANTHROPIC_API_KEY"] = vendor.token
        base = str(vendor.config.get("base_url") or "")
        if base:
            env["ANTHROPIC_BASE_URL"] = base
    else:
        env["ANTHROPIC_AUTH_TOKEN"] = vendor.token
        env["ANTHROPIC_BASE_URL"] = str(vendor.config.get("base_url") or "")

    if ctx.jira is not None:
        env["JIRA_URL"] = str(ctx.jira.config.get("url") or "")
        env["JIRA_USERNAME"] = str(ctx.jira.config.get("username") or "")
        # run_skill.sh prefers JIRA_EMAIL (falls back to JIRA_USERNAME itself) and
        # derives JIRA_USER_EFFECTIVE; mcp.json consumes JIRA_USERNAME directly.
        env["JIRA_EMAIL"] = env["JIRA_USERNAME"]
        env["JIRA_API_TOKEN"] = ctx.jira.token

    # The per-step context travels as a compact env copy — the runner writes
    # $BB_CONTEXT to $CONTEXT_FILE inside the container (Phase 1 dropped the
    # /ctx bind mount).
    env["BB_CONTEXT"] = json.dumps(context, separators=(",", ":"))
    env["CONTEXT_FILE"] = "/home/node/context.json"
    return env


def _allowed_models(vendor) -> list[str]:
    """BB_ALLOWED_MODELS for a run = the integration's resolved tier mappings."""
    config = vendor.config or {}
    return [str(config[k]) for k in ("model_high", "model_medium", "model_low")
            if config.get(k)]
