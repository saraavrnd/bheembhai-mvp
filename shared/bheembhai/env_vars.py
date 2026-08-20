"""Environment-variable domain rules — shared by the platform (save-time
validation, ref paths) and the engine (merge + resolution at run init).

Scope + override model: every variable is either platform-scoped (one global
row, project_id NULL) or project-scoped. A project row sharing a platform
row's name is the override — `merge_env_var_rows` resolves platform-first so
project rows win.

Secret model (ADR-012): secret rows store an opaque SecureStorage ref under
`/bheembhai/env/...` (inside the IAM `parameter/bheembhai/${environment}/*`
policy) — never the raw value. The engine resolves refs fresh at run init.

Reserved names: the engine and the agent runner own a fixed set of env keys
(the git/model/vendor/jira/context/launch-channel groups and the host debug
knobs forwarded by `env_forward`). Users cannot set those — save-time 400 —
and as defense in depth the engine bundle also wins over user vars at
injection. The two `BB_MAX_*` guardrail knobs are the deliberate exception:
user-settable, exported to the container AND consumed by the engine.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bheembhai.models.environment import EnvironmentVariable

ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Keys owned by the engine / agent runner (env bundle, per-attempt launch
# channels, and DockerRuntime.env_forward host debug knobs). Users cannot
# shadow these — the platform rejects them at save time and the engine
# bundle wins at injection regardless.
RESERVED_NAMES: frozenset[str] = frozenset({
    # Engine group
    "RUN_ID", "STEP_ID", "ATTEMPT_NO", "SKILL", "RESULT_DIR", "STORY_ID",
    # Git group
    "BB_GIT_MODE", "GIT_REMOTE_URL", "GIT_SOURCE_BRANCH", "RUN_BRANCH",
    "GH_TOKEN",
    # Model group
    "BB_MODEL", "BB_ALLOWED_MODELS",
    # Vendor keys (Claude Code auth)
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    # Jira / MCP
    "JIRA_URL", "JIRA_USERNAME", "JIRA_EMAIL", "JIRA_API_TOKEN",
    # Context channel
    "BB_CONTEXT", "CONTEXT_FILE",
    # Per-attempt launch channels (presigned URLs — bearer credentials)
    "BB_SKILL_URL", "BB_SKILL_SHA256",
    "BB_RESULT_PUT_URL", "BB_PROGRESS_PUT_URL", "BB_LOG_PUT_URL",
    "BB_DIAG_PUT_URL",
    # Host debug knobs forwarded by DockerRuntime.env_forward (engine process
    # env overlays the container env AFTER the bundle — a user var of the same
    # name would be silently clobbered, so reject up front)
    "BB_MOCK", "BB_MOCK_SECONDS", "BB_MOCK_FORCE",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_EFFORT_LEVEL",
})

# Engine guardrail knobs users MAY set. Exported to the container (harmless —
# run_skill.sh ignores them) and consumed by the engine per-run: they
# override EngineConfig.max_step_visits / max_attempts for the run.
TUNED_NAMES: frozenset[str] = frozenset({"BB_MAX_STEP_VISITS", "BB_MAX_ATTEMPTS"})


def validate_env_var_name(name: str) -> str:
    """Return the name if it's a legal, non-reserved env-var name.

    Raises ValueError with a user-facing reason otherwise: bad characters or
    an engine-owned key.
    """
    if not ENV_VAR_NAME_RE.match(name):
        raise ValueError(
            f"invalid environment variable name '{name}' — use letters, "
            "digits and underscores, and do not start with a digit")
    if name in RESERVED_NAMES:
        raise ValueError(
            f"environment variable '{name}' is owned by the engine — "
            "cannot be set as an environment variable")
    return name


def validate_tunable_value(name: str, value: str) -> None:
    """Guardrail knobs must be positive integers — the engine reads them as
    ints and falls back to its default on garbage, but failing at save time
    keeps surprises out of production runs."""
    if name in TUNED_NAMES:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"environment variable '{name}' must be a positive integer") from exc
        if parsed < 1:
            raise ValueError(
                f"environment variable '{name}' must be a positive integer")


def env_var_ref(project_id: str | None, name: str) -> str:
    """SecureStorage ref for a secret env var (ADR-012 naming convention —
    matches the IAM policy prefix parameter/bheembhai/${environment}/*)."""
    scope = "platform" if project_id is None else str(project_id)
    return f"/bheembhai/env/{scope}/{name}"


def merge_env_var_rows(rows) -> dict[str, EnvironmentVariable]:
    """Platform-first merge: platform rows land first (sorted by name), then
    project rows — so a project row with the same name as a platform row
    (the override) replaces it. Deterministic; preserves insertion order."""
    merged: dict[str, EnvironmentVariable] = {}
    for row in sorted(rows, key=lambda r: (r.scope == "project", r.name)):
        merged[row.name] = row
    return merged
