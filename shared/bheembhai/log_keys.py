"""Log-artifact key scheme — shared by the engine (upload) and the platform (read).

One deterministic, hierarchical key per attempt log:

    logs/<run_id>/<step_id>/<attempt_no>/<kind-file>

Built by a SINGLE function so the writer and the reader can never drift.
Properties (ADR-011):
- idempotent: re-uploading the same attempt overwrites the same key;
- hierarchical: LIST with a run prefix returns exactly that run's logs;
- timestamp-free: stable across crash-recovery re-entry.

``run_id`` is the run UUID (hex, dash-separated — safe as-is). ``step_id``
comes from workflow YAML, whose charset is validated at parse time
(engine_service/workflow.py) — ``_slug`` below is a belt-and-braces sanitizer
so a legacy/odd id can never escape the attempt directory in the key.
"""

import re

# Per-attempt artifacts, in canonical order. Value = filename in the attempt
# directory (engine-side BB_WORKDIR/results/<run_id>/<step_id>/<attempt_no>/).
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


def log_key(run_id: str, step_id: str, attempt_no: int, kind: str) -> str:
    """The object-store key for one attempt's log artifact."""
    if kind not in KIND_FILES:
        raise ValueError(f"unknown log kind {kind!r} (expected one of {KINDS})")
    return f"logs/{run_id}/{_slug(step_id)}/{int(attempt_no)}/{KIND_FILES[kind]}"
