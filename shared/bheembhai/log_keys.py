"""Artifact key scheme — shared by the engine, the platform, and the agent.

Two deterministic, hierarchical namespaces:

    results/<run_id>/<step_id>/<attempt_no>/<channel-file>   — live step channels
    logs/<run_id>/<step_id>/<attempt_no>/<kind-file>         — attempt logs

Built by a SINGLE function per key so the writer and the reader can never
drift. The agent uploads into these keys via presigned PUT URLs (ADR-014);
the engine reads them back; the platform serves them from RunLog rows.

Properties (ADR-011 / ADR-014):
- idempotent: re-uploading the same attempt overwrites the same key;
- hierarchical: LIST with a run prefix returns exactly that run's artifacts;
- timestamp-free: stable across crash-recovery re-entry — a rebuilt Handle
  derives the same keys from (run_id, step_id, attempt_no) with no disk state.

``run_id`` is the run UUID (hex, dash-separated — safe as-is). ``step_id``
comes from workflow YAML, whose charset is validated at parse time
(engine_service/workflow.py) — ``_slug`` below is a belt-and-braces sanitizer
so a legacy/odd id can never escape the attempt directory in the key.
"""

import re

# The orchestrator's control-plane result file. Deliberately NOT "result.json":
# the PDLC skills use result.json as their own in-repo handoff artifact, and an agent
# will happily overwrite a file by that name. Keep the control plane in its own namespace.
RESULT_FILENAME = "bb_step_result.json"
PROGRESS_FILENAME = "progress.json"

# Per-attempt log artifacts, in canonical order. Value = filename at the key
# tail (the agent stages them in its container-local /out dir before upload).
KIND_FILES: dict[str, str] = {
    "agent": "agent.log",
    "container": "container.log",
    "diagnostics": "diagnostics.txt",
}

KINDS: tuple[str, ...] = tuple(KIND_FILES)

_SLUG_BAD = re.compile(r"[^a-z0-9-]+")
_SLUG_MAX = 64


def _slug(step_id: str) -> str:
    """Fold an arbitrary step id into a key-safe token (lowercase alnum + dashes)."""
    folded = _SLUG_BAD.sub("-", (step_id or "").lower()).strip("-")
    return folded[:_SLUG_MAX].rstrip("-") or "step"


def attempt_key_base(run_id: str, step_id: str, attempt_no: int) -> str:
    """Base key for one attempt's live channels (result payload, progress)."""
    return f"results/{run_id}/{_slug(step_id)}/{int(attempt_no)}"


def result_key(run_id: str, step_id: str, attempt_no: int) -> str:
    """The object-store key for one attempt's result payload."""
    return f"{attempt_key_base(run_id, step_id, attempt_no)}/{RESULT_FILENAME}"


def progress_key(run_id: str, step_id: str, attempt_no: int) -> str:
    """The object-store key for one attempt's progress heartbeat."""
    return f"{attempt_key_base(run_id, step_id, attempt_no)}/{PROGRESS_FILENAME}"


def log_key(run_id: str, step_id: str, attempt_no: int, kind: str) -> str:
    """The object-store key for one attempt's log artifact."""
    if kind not in KIND_FILES:
        raise ValueError(f"unknown log kind {kind!r} (expected one of {KINDS})")
    return f"logs/{run_id}/{_slug(step_id)}/{int(attempt_no)}/{KIND_FILES[kind]}"
