"""Run endpoints — list, create, view, and gate decisions for pipeline runs."""

from __future__ import annotations

import logging
import re
import uuid as _uuid
from typing import TYPE_CHECKING

from bheembhai.database import get_session
from bheembhai.log_keys import KINDS
from bheembhai.models.project import ProjectIntegration
from bheembhai.models.run import Run, RunLog, Step, Transition
from bheembhai.models.user import Membership, User
from bheembhai.models.work_queue import WorkQueueItem
from bheembhai.models.workflow import Policy, Workflow
from bheembhai.protocols.auth import Identity
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from platform_api.agent_log import LogBlock, parse_agent_log
from platform_api.dependencies import get_current_enabled_user
from platform_api.github_content import build_chain, git_fetch_content, resolve_step_sha
from platform_api.routers._integration_shared import AI_VENDOR_TYPES
from platform_api.routers._workflow_shared import (
    _parse_workflow_yaml,
    clone_referenced_skills,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

# Mirrors engine_service.state_machine.TERMINAL_STATES — the run states after
# which no dispatch token (decision or cancel) has anything to act on.
TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


# ── Schemas ────────────────────────────────────────────────────────────────────


class RunCreateRequest(BaseModel):
    project_id: str = Field(..., min_length=1)
    workflow_id: str = Field(..., min_length=1)
    policy_id: str | None = None
    # Required for workflow runs; ignored for ad-hoc sessions.
    story_id: str | None = Field(None, min_length=1)
    github_integration_id: str = Field(..., min_length=1)
    jira_integration_id: str | None = None
    ai_vendor_integration_id: str = Field(..., min_length=1)
    # Per-run override for the branch the engine cuts the run branch OFF
    # (defaults to the GitHub integration's base_branch — ADR-013 deferred item).
    # For ad-hoc runs this is the REQUIRED user-named branch to work on directly.
    source_branch: str | None = Field(None, max_length=200)
    # Ad-hoc sessions (ADR-016): "adhoc" runs the user's query verbatim on
    # their branch; the default "workflow" is the governed pipeline.
    run_kind: str = Field("workflow", pattern="^(workflow|adhoc)$")
    query: str | None = Field(None, description="User query (required for ad-hoc runs)")


class DecisionRequest(BaseModel):
    action: str = Field(..., description="'approve' or 'send_back'")
    send_back_to: str | None = Field(None, description="Step ID to revert to (required for send_back)")
    comment: str | None = Field(None, description="Reviewer comment")


class TurnRequest(BaseModel):
    query: str = Field(..., description="The user's next message to the session")


_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def _valid_source_branch(name: str) -> str | None:
    """Human-readable problem for an invalid source-branch name, else None.

    Mirrors git check-ref-format's essentials — the engine will fail at
    init anyway if the branch can't be resolved, but a clear 422 at
    submit time beats a container-less failure ten seconds later.
    """
    if name != name.strip():
        return "branch must not have leading/trailing whitespace"
    if name.startswith("-") or name.endswith(("/", ".")):
        return "branch must not start with '-' or end with '/' or '.'"
    if ".." in name or "@{" in name or "//" in name:
        return "branch must not contain '..', '@{', or '//'"
    if name.endswith(".lock") or any(c in name for c in " ~^:?*[\\"):
        return "branch contains characters git does not allow in refs"
    if not _BRANCH_NAME_RE.fullmatch(name):
        return "branch contains characters git does not allow in refs"
    return None


# ── Helpers ────────────────────────────────────────────────────────────────────


def _user_dict(user: User | None) -> dict | None:
    """Public identity of a run initiator — never expose internal user fields."""
    if user is None:
        return None
    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
    }


def _run_summary(run: Run, started_by: User | None = None) -> dict:
    """Lightweight run for list views."""
    return {
        "id": str(run.id),
        "project_id": str(run.project_id),
        "workflow_id": str(run.workflow_id),
        "policy_id": str(run.policy_id),
        "story_id": run.story_id,
        "run_kind": run.run_kind,
        "user_query": run.user_query,
        "state": run.state,
        # Ad-hoc sessions (ADR-016 §2): the reaper's lifecycle clock.
        "session_phase": run.session_phase,
        "session_last_activity_at": run.session_last_activity_at.isoformat()
        if run.session_last_activity_at else None,
        "current_step": run.current_step,
        "cost_usd": float(run.cost_usd),
        "created_at": run.created_at.isoformat() if run.created_at else "",
        "source_branch": run.source_branch,
        "run_branch": run.run_branch,
        "started_by": _user_dict(started_by),
        "github_integration_id": str(run.github_integration_id) if run.github_integration_id else None,
        "jira_integration_id": str(run.jira_integration_id) if run.jira_integration_id else None,
        "ai_vendor_integration_id": str(run.ai_vendor_integration_id) if run.ai_vendor_integration_id else None,
    }


async def _require_verified_integration(
    db: AsyncSession,
    project_id: str,
    integration_id: str,
    expected_types: set[str],
    label: str,
) -> ProjectIntegration:
    """Validate an integration selected for a run.

    The integration must belong to the project, be of an expected type, and
    have passed its connection test (``verified_at`` set). The run modal only
    offers verified integrations; this is the server-side enforcement.
    """
    try:
        integ = await db.get(ProjectIntegration, _uuid.UUID(integration_id))
    except ValueError:
        raise HTTPException(422, f"Invalid {label} integration id: {integration_id}")
    if integ is None or str(integ.project_id) != project_id:
        raise HTTPException(422, f"Selected {label} integration does not belong to this project")
    if integ.type not in expected_types:
        raise HTTPException(422, f"Selected {label} integration is not of the expected type")
    if integ.verified_at is None:
        raise HTTPException(422, f"Selected {label} integration has not passed its connection test")
    return integ


def _parse_workflow_steps(workflow: Workflow | None) -> list[dict]:
    """Parse a workflow's YAML to extract step definitions for the stage rail."""
    if workflow is None or not workflow.yaml_content:
        return []
    import yaml
    try:
        raw = yaml.safe_load(workflow.yaml_content)
    except yaml.YAMLError:
        return []
    if not isinstance(raw, dict):
        return []
    steps = raw.get("steps") or []
    return [
        {
            "id": s.get("id", ""),
            "skill": s.get("skill", ""),
            "label": s.get("label", s.get("id", "")),
            "model": s.get("model", ""),
            "deadline": s.get("deadline", 900),
        }
        for s in steps if isinstance(s, dict)
    ]


def _parse_policy_gates(policy: Policy | None) -> dict[str, dict]:
    """Parse a policy's YAML to extract gate definitions keyed by step_id."""
    if policy is None or not policy.yaml_content:
        return {}
    import yaml
    try:
        raw = yaml.safe_load(policy.yaml_content)
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict):
        return {}
    gates = raw.get("gates") or {}
    result: dict[str, dict] = {}
    for step_id, g in gates.items():
        if isinstance(g, dict):
            result[str(step_id)] = {
                "review": str(g.get("review", "required")),
                "role": str(g.get("role", "any")),
                "on_status": [str(x) for x in g.get("on_status", [])] if g.get("on_status") else None,
            }
    return result


def _step_to_dict(step: Step) -> dict:
    """Single step with state and timing."""
    return {
        "id": str(step.id),
        "step_id": step.step_id,
        "skill": step.skill,
        "exec_state": step.exec_state,
        "result_status": step.result_status,
        "model_requested": step.model_requested,
        "models_used": step.models_used,
        "cost_usd": float(step.cost_usd),
        "attempt_no": step.attempt_no,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "ended_at": step.ended_at.isoformat() if step.ended_at else None,
    }


async def _latest_step_payload(db, run_id, step_id: str) -> dict:
    """Newest display payload for a finished step: the awaiting_approval gate
    card while paused, otherwise the completion payload. Both carry the same
    artifact fields (summary/files/review_files/commit) — the engine owns them,
    the platform only displays them.

    Gate approval is recorded as an ``awaiting_approval→completed`` transition
    with an *empty* payload — skip content-less rows so an approved step still
    renders its gate card / completion result instead of falling back to the
    demo stubs.

    Deliberately NOT filtered by to_state: the engine records verdict rows for
    non-happy results (BLOCK / changes_requested / escalation_required) with
    to_state="failed" (state_machine convention) and those rows carry the
    display payload — same trap family as _summary_payloads and
    resolve_step_sha (the content-key check below is the discriminator)."""
    stmt = (
        select(Transition)
        .where(Transition.run_id == run_id,
               Transition.step_id == step_id)
        .order_by(Transition.id.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    display_keys = ("files", "review_files", "summary", "artifact", "commit")
    for row in rows:
        payload = dict(row.payload or {})
        if any(k in payload for k in display_keys):
            return payload
    # No content-bearing row (e.g. the only row is the empty approval record)
    # — callers fall back to their demo-stub behavior.
    return {}


async def _summary_payloads(db, run_id, step_id: str) -> list[dict]:
    """Newest-first payloads of a finished step that carry a summary —
    completion rows carry ``summary_full``, gate-card rows only the truncated
    ``summary`` (the full text lives on the slightly older completion row).

    Deliberately NOT filtered by to_state: the engine records verdict rows for
    non-happy results (BLOCK / changes_requested / escalation_required) with
    to_state="failed" (state_machine convention), and those rows carry the
    summary too — the same content-bearing rows the poll builder displays. A
    to_state filter here once 404'd the full-summary endpoint for an
    escalation row (run 07c4b440)."""
    stmt = (
        select(Transition)
        .where(Transition.run_id == run_id,
               Transition.step_id == step_id)
        .order_by(Transition.id.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    out: list[dict] = []
    for row in rows:
        payload = dict(row.payload or {})
        if "summary" in payload or "summary_full" in payload:
            out.append(payload)
    return out


def _pick_full_summary(payloads: list[dict], commit: str | None):
    """Choose the reviewer-visible summary from newest-first payloads.

    Returns ``(text, used_commit, truncated)``. A visit-pinning ``commit``
    (multi-visit loops store a different SHA per visit) picks that visit's
    text; otherwise the newest full text wins; otherwise the newest truncated
    head (pre-feature runs) with ``truncated=True``.
    """
    if commit:
        fallback = None
        for p in payloads:
            if p.get("commit") == commit:
                full = p.get("summary_full")
                if full:
                    return str(full), commit, False
                # Keep scanning: the newest matching row may be a gate card
                # (truncated head, no full) while the completion row just
                # behind it carries the full text for the same commit.
                if fallback is None and p.get("summary"):
                    fallback = str(p["summary"])
        if fallback is not None:
            return fallback, commit, True
    for p in payloads:
        if p.get("summary_full"):
            return str(p["summary_full"]), p.get("commit"), False
    for p in payloads:
        if p.get("summary"):
            return str(p["summary"]), p.get("commit"), True
    return None, None, True


def _viewer_for_path(path: str) -> str:
    """Viewer type from extension — matches the viewer vocabulary in run_detail.html."""
    lower = path.lower()
    if lower.endswith((".diff", ".patch")):
        return "diff"
    if lower.endswith(".csv"):
        return "table"
    if lower.endswith(".json"):
        return "json"
    if lower.endswith((".yaml", ".yml", ".py", ".ts", ".js", ".sh", ".toml", ".txt", ".env")):
        return "code"
    if lower.endswith(".md") and "comment" in lower:
        return "comments"
    return "doc"


def _files_from_payload(payload: dict) -> list[dict]:
    """Artifact pills from an engine payload. Curated review_files (the skill's
    BB_REVIEW declarations) come first per the curation design; if the skill
    declared none, fall back to the full changed-file list. Empty when the
    engine published neither — callers may then fall back to demo stubs."""
    curated = [f for f in (payload.get("review_files") or []) if isinstance(f, dict)]
    changed = [f for f in (payload.get("files") or []) if isinstance(f, dict)]
    pills: list[dict] = []
    for item in curated or changed:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        pills.append({
            "path": path,
            "label": path.rsplit("/", 1)[-1],
            "note": str(item.get("note") or ""),
            "size": "",
            "viewer": _viewer_for_path(path),
        })
    return pills


# ── Execution timeline ─────────────────────────────────────────────────────────
#
# The stage rail used to show each workflow step ONCE, merged with the step
# row's LATEST state. That lies about loops: when code-review returns
# changes_requested and the workflow routes back to implement, the implement
# row shows only the re-run's failure — the earlier visit (which test-verify
# actually verified) disappears from view. The transition stream is
# append-only and complete, so the timeline is rebuilt from it: each
# `pending→running` row starts a visit (attempt_no does NOT distinguish
# visits — re-loops reuse the same attempt dir), and visits are laid out in
# true execution order with their own verdict, artifacts, and gate decisions.

_DISPLAY_PAYLOAD_KEYS = ("files", "review_files", "summary", "artifact", "commit")


def _fmt_elapsed(started: float, ended: float) -> str:
    import datetime as _dt
    return str(_dt.timedelta(seconds=int(ended - started)))


def _gate_decision_from(row) -> dict | None:
    """Parse a decision transition into {action, actor, comment, ts}.

    The engine records decisions as ``awaiting_approval→completed`` rows with
    reason ``reviewer chose: approve — <comment>`` (state_machine.py) and the
    reviewer in the actor column."""
    reason = (row.reason or "").strip()
    if reason.startswith("reviewer chose: approve"):
        action = "approve"
    elif reason.startswith("reviewer chose: send back"):
        action = "send_back"
    else:
        return None
    comment = None
    if " — " in reason:
        comment = reason.split(" — ", 1)[1].strip() or None
    return {"action": action, "actor": row.actor, "comment": comment, "ts": float(row.ts)}


def _log_map(log_rows) -> dict:
    """{(step_id, attempt_no): {"size": total bytes, "kinds": sorted kinds}}
    from run_logs reference rows — the poll payload's has_logs source."""
    out: dict = {}
    for row in log_rows or []:
        entry = out.setdefault((row.step_id, row.attempt_no), {"size": 0, "kinds": []})
        entry["size"] += row.size_bytes or 0
        entry["kinds"].append(row.kind)
    for entry in out.values():
        entry["kinds"].sort()
    return out


def _build_timeline(rows, workflow_def: list[dict], gates: dict[str, dict],
                    run_state: str, log_rows=None) -> dict:
    """Rebuild the true execution path from the transition stream.

    Pure over duck-typed transition rows (id-ordered) so the visit-grouping
    logic is unit-testable without a database. Returns
    ``{nodes: [...], current_node_idx}`` — one node per visit in execution
    order, plus unvisited workflow steps as a pending tail.

    ``log_rows``: run_logs reference rows for the run (never content) — each
    visit node carries has_logs/log_size/log_kinds so the UI can enable the
    stage-log viewer without probing storage per poll.
    """
    logs = _log_map(log_rows)
    visits: list[dict] = []
    open_visit: dict | None = None
    by_step: dict[str, int] = {}

    for row in rows:
        if not row.step_id:
            continue  # run-level bookkeeping (init, model resolution, resets)
        if row.from_state == "pending" and row.to_state == "running":
            if (open_visit is not None and open_visit["step_id"] == row.step_id
                    and open_visit["verdict"] is None
                    and open_visit["ended_ts"] is None):
                # The SAME visit re-announced: a deadline retry ("transient …
                # — retrying") or a crash-recovery resume both re-fire
                # pending→running while the step never closed. Merging keeps
                # one visit; splitting here created phantom open visits that
                # rendered as running forever (run b2b1b72a's five resume
                # rows for test-creator). A visit that HAS ended (verdict or
                # gate) still opens a new one — that's the real re-loop case.
                continue
            if open_visit is not None:
                visits.append(open_visit)
            visit_no = by_step.get(row.step_id, 0) + 1
            by_step[row.step_id] = visit_no
            open_visit = {
                "step_id": row.step_id, "visit_no": visit_no,
                "started_ts": float(row.ts), "ended_ts": None,
                "verdict": None, "reason": None,
                "gate_open": False, "gate_decision": None,
                "payload": None, "attempt_no": row.attempt_no or 1,
            }
            continue
        v = open_visit
        if v is None or v["step_id"] != row.step_id:
            # A failure row addressed to a step whose visit already closed —
            # the engine's run-level failure rows land here (e.g. the
            # runaway-loop cap: "step 'implement' visited 4 times in one
            # dispatch (cap 3)"). Surface it as a halted node instead of
            # dropping it, so the run's failure reason stays visible.
            if row.to_state == "failed" and row.from_state == "running":
                if open_visit is not None:
                    visits.append(open_visit)
                    open_visit = None
                visit_no = by_step.get(row.step_id, 0) + 1
                by_step[row.step_id] = visit_no
                visits.append({
                    "step_id": row.step_id, "visit_no": visit_no,
                    "started_ts": None, "ended_ts": None,
                    "verdict": None, "reason": row.reason,
                    "gate_open": False, "gate_decision": None,
                    "payload": None, "attempt_no": row.attempt_no or 1,
                    "halt": True,
                })
            continue
        if row.to_state == "awaiting_approval":
            # The gate card is the visit's effective end; the wait itself
            # surfaces as the decision edge (or the open review footer).
            v["gate_open"] = True
            v["ended_ts"] = float(row.ts)
            continue
        if row.from_state == "awaiting_approval" and row.to_state == "completed":
            decision = _gate_decision_from(row)
            if decision:
                v["gate_decision"] = decision
            elif (row.reason or "").startswith("gate closed — run cancelled"):
                # Stop-run while the gate was open: the engine must close the
                # awaiting transition with a terminal state and reuses
                # 'completed' (worker.py _cancel_guarded) — but this is NOT an
                # approval. Record it as a cancel decision so the visit
                # renders as cancelled, not as done/completed.
                v["gate_decision"] = {
                    "action": "cancel",
                    "actor": row.actor,
                    "comment": None,
                    "ts": float(row.ts),
                }
            continue
        if (row.to_state in ("completed", "failed")
                and row.result_status and v["verdict"] is None):
            # First terminal row carrying a verdict: the payload's own reason
            # (e.g. "could not clone … @ main") outranks the engine's 'ok'.
            v["verdict"] = row.result_status
            v["ended_ts"] = float(row.ts)
            v["reason"] = None if row.reason in (None, "ok") else row.reason
            payload = dict(row.payload or {})
            if payload.get("reason"):
                v["reason"] = str(payload["reason"])
        payload = dict(row.payload or {})
        if any(k in payload for k in _DISPLAY_PAYLOAD_KEYS):
            # Newest content-bearing row in the visit (approval rows are empty
            # and never overwrite the real payload).
            v["payload"] = payload
    if open_visit is not None:
        visits.append(open_visit)

    wf_by_id = {s["id"]: s for s in workflow_def}
    nodes: list[dict] = []
    for v in visits:
        wf = wf_by_id.get(v["step_id"], {})
        verdict = v["verdict"]
        if v.get("halt"):
            state = "failed"
        elif verdict == "__cancelled__":
            state = "cancelled"
        elif verdict and verdict.startswith("failed_"):
            state = "failed"
        elif v["gate_open"] and not v["gate_decision"] and run_state == "paused":
            state = "awaiting"
        elif (v["gate_open"] and run_state == "cancelled"
              and v["gate_decision"] and v["gate_decision"].get("action") == "cancel"):
            # The gate was closed by a stop-run while waiting for review —
            # the visit's own verdict (completed) stays, but the REVIEW was
            # cut short, so the stage reads cancelled, not done.
            state = "cancelled"
        elif run_state == "cancelled" and verdict is None:
            # The visit that was open when the run was cancelled (no step-level
            # verdict row exists — e.g. cancel landed between steps).
            state = "cancelled"
        elif verdict:
            state = "done"
        else:
            state = "current"
        payload = v["payload"] or {}
        summary_text = str(payload.get("summary") or "")
        nodes.append({
            "step_id": v["step_id"],
            "label": wf.get("label", v["step_id"]),
            "skill": wf.get("skill", ""),
            "has_gate": gates.get(v["step_id"], {}).get("review") == "required",
            "visit_no": v["visit_no"],
            "state": state,
            "verdict": verdict,
            "reason": v["reason"],
            "elapsed": _fmt_elapsed(v["started_ts"], v["ended_ts"]) if v["ended_ts"] else None,
            "started_ts": v["started_ts"],
            "ended_ts": v["ended_ts"],
            "files": _files_from_payload(payload),
            "summary": summary_text,
            # The DB keeps the FULL summary (summary_full); the poll payload
            # stays light with just the 1500-char head. The flag tells the UI
            # to offer a "load full summary" fetch instead of sending the
            # whole text down the wire every poll.
            "has_full_summary": len(str(payload.get("summary_full") or "")) > len(summary_text),
            "commit": payload.get("commit"),
            "cost_usd": payload.get("cost_usd"),
            "cost_reported": bool(payload.get("cost_reported")),
            "cost_partial": bool(payload.get("cost_partial")),
            "is_awaiting_review": state == "awaiting",
            "gate_decision": v["gate_decision"],
            "attempt_no": v["attempt_no"],
            "has_logs": (v["step_id"], v["attempt_no"]) in logs,
            "log_size": logs.get((v["step_id"], v["attempt_no"]), {}).get("size"),
            "log_kinds": logs.get((v["step_id"], v["attempt_no"]), {}).get("kinds", []),
        })

    # Tail: workflow steps that never ran (future nodes, shown pending).
    visited = {v["step_id"] for v in visits}
    for wf in workflow_def:
        if wf["id"] in visited:
            continue
        nodes.append({
            "step_id": wf["id"],
            "label": wf.get("label", wf["id"]),
            "skill": wf.get("skill", ""),
            "has_gate": gates.get(wf["id"], {}).get("review") == "required",
            "visit_no": 0,
            "state": "pending",
            "verdict": None,
            "reason": None,
            "elapsed": None,
            "started_ts": None,
            "ended_ts": None,
            "files": [],
            "summary": "",
            "has_full_summary": False,
            "commit": None,
            "cost_usd": None,
            "cost_reported": False,
            "cost_partial": False,
            "is_awaiting_review": False,
            "gate_decision": None,
            "attempt_no": 0,
            "has_logs": False,
            "log_size": None,
            "log_kinds": [],
        })

    current_node_idx = next(
        (i for i, n in enumerate(nodes) if n["state"] in ("current", "awaiting")), None)
    return {"nodes": nodes, "current_node_idx": current_node_idx}


async def _run_timeline(db, run: Run, workflow_def: list[dict],
                        gates: dict[str, dict]) -> dict:
    """Fetch the run's transition stream and rebuild the execution timeline."""
    rows = (await db.execute(
        select(Transition)
        .where(Transition.run_id == run.id)
        .order_by(Transition.id)
    )).scalars().all()
    log_rows = (await db.execute(
        select(RunLog).where(RunLog.run_id == run.id)
    )).scalars().all()
    return _build_timeline(rows, workflow_def, gates, run.state, log_rows)


async def _session_turns(db, run: Run) -> list[dict]:
    """Ad-hoc session turn history (ADR-016 §2) — the chat transcript, rebuilt
    from the Transition stream: durable, auditable, independent of object
    storage. One entry per COMPLETED or failed turn (kind=="turn" completion
    rows), newest last. A turn in flight shows as run.state == "running" and
    has no entry yet."""
    rows = (await db.execute(
        select(Transition)
        .where(Transition.run_id == run.id)
        .order_by(Transition.id)
    )).scalars().all()
    turns = []
    for t in rows:
        p = t.payload or {}
        if p.get("kind") != "turn":
            continue
        turns.append({
            "seq": p.get("seq"),
            "query": p.get("query", ""),
            "response": p.get("response", ""),
            "result_status": p.get("result_status"),
            "commit": p.get("commit"),
            "files": p.get("files") or [],
            "cost_usd": p.get("cost_usd"),
            "cost_reported": bool(p.get("cost_reported")),
            "ts": float(t.ts) if t.ts else None,
        })
    return turns


async def _build_run_detail(db, run: Run, started_by: User | None = None) -> dict:
    """Full run detail with steps, workflow definition, and gate map."""
    workflow_def = _parse_workflow_steps(run.workflow) if run.workflow else []
    gates = _parse_policy_gates(run.policy) if run.policy else {}

    # Build step map keyed by workflow step_id for merging
    db_step_map: dict[str, dict] = {}
    for s in (run.steps or []):
        db_step_map[s.step_id] = _step_to_dict(s)

    # Merge workflow definition with DB step state → stage rail entries
    stages: list[dict] = []
    current_idx: int | None = None
    for i, wf_step in enumerate(workflow_def):
        sid = wf_step["id"]
        db_step = db_step_map.get(sid, {})
        gate = gates.get(sid)

        # Determine visual state
        exec_state = db_step.get("exec_state", "pending")
        result_status = db_step.get("result_status")

        if exec_state == "completed" and result_status in ("completed", None):
            visual_state = "done"
        elif exec_state in ("running", "pending_review"):
            visual_state = "current"
            current_idx = i
        elif exec_state == "failed" or (result_status or "").startswith("failed_"):
            visual_state = "failed"
        else:
            visual_state = "pending"

        # Determine if this stage has a review gate
        has_gate = gate is not None and gate.get("review") == "required"
        is_awaiting_review = (
            has_gate
            and exec_state == "completed"
            and result_status == "completed"
            and run.state == "paused"
            and run.current_step == sid
        )

        # Elapsed duration for completed/running steps
        elapsed = None
        if db_step.get("started_at") and db_step.get("ended_at"):
            import datetime as _dt
            started = _dt.datetime.fromisoformat(db_step["started_at"])
            ended = _dt.datetime.fromisoformat(db_step["ended_at"])
            elapsed = str(ended - started).split(".")[0]  # "0:03:10"

        # Artifacts come from the engine's transition payload (curated review
        # files first, full changed-file list as fallback). Stubs remain only
        # for legacy/demo runs where the engine published nothing.
        payload = await _latest_step_payload(db, run.id, sid)
        files = _files_from_payload(payload)
        if not files:
            files = _stub_files_for_step(sid, exec_state, result_status)

        stages.append({
            "step_id": sid,
            "skill": wf_step["skill"],
            "label": wf_step["label"],
            "model": wf_step.get("model", ""),
            "deadline": wf_step.get("deadline", 900),
            "visual_state": visual_state,           # done | current | failed | pending
            "exec_state": exec_state,
            "result_status": result_status,
            "has_gate": has_gate,
            "is_awaiting_review": is_awaiting_review,
            "attempt_no": db_step.get("attempt_no", 1),
            "elapsed": elapsed,
            "cost_usd": db_step.get("cost_usd", 0),
            "files": files,
            "summary": str(payload.get("summary") or ""),
            "commit": payload.get("commit"),
        })

    # Build the full detail response
    timeline = await _run_timeline(db, run, workflow_def, gates)
    turns = await _session_turns(db, run) if run.run_kind == "adhoc" else []
    return {
        **{k: _run_summary(run, started_by)[k] for k in ["id", "project_id", "workflow_id", "policy_id", "story_id", "run_kind", "user_query", "state", "current_step", "cost_usd", "created_at", "started_by", "github_integration_id", "jira_integration_id", "ai_vendor_integration_id", "session_phase", "session_last_activity_at"]},
        "source_branch": run.source_branch,
        "run_branch": run.run_branch,
        "turns": turns,
        "workflow_name": run.workflow.name if run.workflow else "",
        "policy_name": run.policy.name if run.policy else "",
        "stages": stages,
        "current_stage_idx": current_idx,
        # True execution path: one node per visit in execution order (loops
        # re-open a step as a new visit). `stages` stays for the send-back
        # modal, which picks unique workflow steps by id.
        "timeline": timeline,
        "elapsed_total": _compute_elapsed(run.created_at),
        "needs_review": run.state == "paused",
    }


def _compute_elapsed(created_at) -> str | None:
    """Human-readable elapsed time since creation."""
    if created_at is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    delta = now - created_at
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    return f"{hrs}h {mins % 60}m ago"


def _stub_files_for_step(step_id: str, exec_state: str,
                         result_status: str | None = None) -> list[dict]:
    """Generate stub file artifacts for demo purposes.

    In production these are looked up from artifact storage via ``artifact_storage_key``.
    A step whose verdict is in the failed_* family produced nothing — never show
    artifacts for it (its row may still read exec_state="completed", see the engine).
    """
    if exec_state not in ("completed", "pending_review"):
        return []
    if (result_status or "").startswith("failed_"):
        return []

    # These are demo file stubs — each maps to a viewer type
    if step_id == "story-design":
        return [
            {"path": "story.md", "label": "story.md", "size": "1.2KB", "viewer": "doc"},
            {"path": "design-notes.md", "label": "design-notes.md", "size": "0.8KB", "viewer": "doc"},
        ]
    elif step_id == "test-creator":
        return [
            {"path": "test-plan.csv", "label": "test-plan.csv", "size": "2.1KB", "viewer": "table"},
            {"path": "new-tests.diff", "label": "new-tests.diff", "size": "6.4KB", "viewer": "diff"},
        ]
    elif step_id == "implement":
        return [
            {"path": "changes.diff", "label": "changes.diff", "size": "14.2KB", "viewer": "diff"},
        ]
    elif step_id == "test-verify":
        return [
            {"path": "test-results.csv", "label": "test-results.csv", "size": "3.1KB", "viewer": "table"},
        ]
    elif step_id == "code-review":
        return [
            {"path": "review-comments.md", "label": "review-comments.md", "size": "1.7KB", "viewer": "comments"},
            {"path": "reviewed.diff", "label": "reviewed.diff", "size": "11.0KB", "viewer": "diff"},
        ]
    elif step_id == "pr-create":
        return [
            {"path": "pr-summary.md", "label": "pr-summary.md", "size": "0.5KB", "viewer": "doc"},
        ]
    return []


# ── Stub file content (demo viewer data) ──────────────────────────────────────


_STUB_FILE_CONTENT: dict[str, str] = {
    "story.md": (
        "# Story: Implement SSO Login via Okta\n\n"
        "- As a platform user, I want to log in via Okta SSO so that I can use my company credentials\n"
        "- Acceptance criteria:\n"
        "  - SAML 2.0 assertion flow with Okta as IdP\n"
        "  - Just-in-time user provisioning on first login\n"
        "  - Session timeout matches Okta session policy\n"
        "  - Graceful fallback when Okta is unreachable (local auth)\n\n"
        "## Scope\n"
        "- New module: `src/auth/okta_client.py`\n"
        "- Migration: add `okta_id` column to users table\n"
        "- Docs: update ops runbook with Okta setup\n"
    ),
    "review-comments.md": (
        "# Code Review — SSO Login via Okta\n\n"
        "## S1  src/auth/okta_client.py:34\n"
        "Discovery document is cached for 24h but never revalidated on issuer rotation. "
        "Rotating your Okta signing keys mid-cache window would break every login for the next 24h.\n\n"
        "## S2  src/auth/session.py:42\n"
        "The provider fallback silently keeps local auth alive after the first Okta success. "
        "If Okta is later deprovisioned for a user, they can still log in locally — the fallback never expires.\n\n"
        "## S3  tests/test_migration.py:18\n"
        "Migration test asserts `okta_id IS NOT NULL` but the column is nullable — the test would pass on an empty table and fail on an existing one.\n\n"
        "## nit src/auth/okta_client.py:12\n"
        "Docstring says 'SAML' but the implementation uses OIDC — pick one and align.\n"
    ),
    "test-results.csv": (
        "test_case,suite,duration,result\n"
        "test_saml_assertion_flow,auth,1.24s,pass\n"
        "test_jit_provisioning,auth,0.87s,pass\n"
        "test_session_timeout,session,2.10s,pass\n"
        "test_okta_unreachable_fallback,resilience,1.55s,pass\n"
        "test_token_refresh_on_expiry,auth,0.93s,fail\n"
    ),
    "test-plan.csv": (
        "test_case,suite,duration,result\n"
        "test_saml_assertion_flow,auth,—,planned\n"
        "test_jit_provisioning,auth,—,planned\n"
        "test_session_timeout,session,—,planned\n"
        "test_okta_unreachable_fallback,resilience,—,planned\n"
        "test_token_refresh_on_expiry,auth,—,planned\n"
    ),
    "changes.diff": (
        "@@ -0,0 +1,45 @@ src/auth/okta_client.py\n"
        "+import httpx\n"
        "+import hashlib\n"
        "+from datetime import datetime, timedelta\n"
        "+\n"
        "+class OktaClient:\n"
        "+    def __init__(self, base_url: str, api_token: str):\n"
        "+        self.base_url = base_url\n"
        "+        self.api_token = api_token\n"
        "+        self._discovery_cache = None\n"
        "+        self._cache_ts = None\n"
        "+\n"
        "+    async def get_discovery_doc(self) -> dict:\n"
        "+        if self._discovery_cache and self._cache_ts:\n"
        "+            if datetime.now() - self._cache_ts < timedelta(hours=24):\n"
        "+                return self._discovery_cache\n"
        "+        async with httpx.AsyncClient() as client:\n"
        "+            resp = await client.get(f\"{self.base_url}/.well-known/openid-configuration\")\n"
        "+            self._discovery_cache = resp.json()\n"
        "+            self._cache_ts = datetime.now()\n"
        "+        return self._discovery_cache\n"
        "+\n"
        "+    async def verify_id_token(self, token: str) -> dict:\n"
        "+        doc = await self.get_discovery_doc()\n"
        "+        jwks_uri = doc.get('jwks_uri')\n"
        "+        # ... token verification logic ...\n"
        "@@ -0,0 +1,32 @@ src/auth/session.py\n"
        "+    async def _okta_fallback(self, user: User) -> AuthResult:\n"
        "+        if not user.okta_id:\n"
        "+            return await self._local_auth(user)\n"
        "+        return await self._okta_auth(user)\n"
        "@@ -18,2 +18,4 @@ tests/test_migration.py\n"
        " def test_okta_column_added():\n"
        "     result = db.execute('SELECT okta_id FROM users LIMIT 1')\n"
        "-    assert result is not None\n"
        "+    # Column exists regardless of nullability\n"
        "+    assert result.returns_rows\n"
    ),
    "new-tests.diff": (
        "@@ -0,0 +1,28 @@ tests/test_okta_auth.py\n"
        "+import pytest\n"
        "+from src.auth.okta_client import OktaClient\n"
        "+\n"
        "+@pytest.mark.asyncio\n"
        "+async def test_saml_assertion_flow():\n"
        "+    client = OktaClient('https://example.okta.com', 'test-token')\n"
        "+    doc = await client.get_discovery_doc()\n"
        "+    assert 'issuer' in doc\n"
        "+    assert 'jwks_uri' in doc\n"
        "+\n"
        "+@pytest.mark.asyncio\n"
        "+async def test_jit_provisioning():\n"
        "+    # Just-in-time user creation on first Okta login\n"
        "+    pass\n"
    ),
    "reviewed.diff": (
        "@@ -12,7 +12,9 @@ src/auth/okta_client.py\n"
        "     async def get_discovery_doc(self) -> dict:\n"
        "         if self._discovery_cache and self._cache_ts:\n"
        "-            if datetime.now() - self._cache_ts < timedelta(hours=24):\n"
        "+            if datetime.now() - self._cache_ts < timedelta(minutes=15):\n"
        "                 return self._discovery_cache\n"
        "         async with httpx.AsyncClient() as client:\n"
        "@@ -22,6 +24,7 @@ src/auth/session.py\n"
        "     async def _okta_fallback(self, user: User) -> AuthResult:\n"
        "         if not user.okta_id:\n"
        "             return await self._local_auth(user)\n"
        "+        # Re-verify Okta status on each fallback attempt\n"
        "         return await self._okta_auth(user)\n"
    ),
    "pr-summary.md": (
        "# PR: Implement SSO Login via Okta\n\n"
        "- SAML 2.0 assertion flow with Okta as IdP\n"
        "- Just-in-time user provisioning\n"
        "- Session timeout aligned with Okta policy\n"
        "- Graceful fallback to local auth\n\n"
        "## Files changed\n"
        "- `src/auth/okta_client.py` — new Okta client\n"
        "- `src/auth/session.py` — Okta fallback in auth chain\n"
        "- `tests/test_okta_auth.py` — new test suite\n"
        "- `migrations/006_add_okta_id.sql` — schema change\n"
    ),
    "design-notes.md": (
        "# Design Notes — SSO Implementation\n\n"
        "- OIDC chosen over SAML for simplicity\n"
        "- Discovery document fetched on first use, cached with configurable TTL\n"
        "- Fallback chain: Okta → local → deny\n"
    ),
}


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("")
async def list_runs(
    project_id: str | None = Query(None),
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> list[dict]:
    """List runs for a project, newest first. Includes workflow step info for progress bars."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    stmt = (
        select(Run)
        .options(selectinload(Run.steps), selectinload(Run.workflow), selectinload(Run.policy))
        .order_by(Run.created_at.desc())
        .limit(100)
    )

    if project_id:
        if current_user.platform_role != "ADMIN":
            # Verify membership (platform ADMINs bypass)
            member_check = await db.execute(
                select(Membership).where(
                    Membership.user_id == current_user.id,
                    Membership.project_id == project_id,
                )
            )
            if member_check.scalar_one_or_none() is None:
                raise HTTPException(403, "You are not a member of this project")
        stmt = stmt.where(Run.project_id == project_id)

    runs_result = await db.execute(stmt)
    runs = runs_result.scalars().unique().all()

    # Resolve initiators in one query (runs reference users by id only)
    starter_ids = {r.started_by_user_id for r in runs if r.started_by_user_id}
    users_map: dict = {}
    if starter_ids:
        users_result = await db.execute(select(User).where(User.id.in_(starter_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}

    results: list[dict] = []
    for run in runs:
        summary = _run_summary(run, started_by=users_map.get(run.started_by_user_id))
        # Add progress info for the run list
        wf_def = _parse_workflow_steps(run.workflow) if run.workflow else []
        gates = _parse_policy_gates(run.policy) if run.policy else {}

        # Build progress per stage
        stage_progress: list[dict] = []
        for wf_step in wf_def:
            sid = wf_step["id"]
            db_step = next((s for s in (run.steps or []) if s.step_id == sid), None)
            if db_step:
                es = db_step.exec_state
                rs = db_step.result_status
                if es == "completed" and rs in ("completed", None):
                    ps = "done"
                elif es in ("running", "pending_review"):
                    ps = "current"
                elif es == "failed" or rs in ("failed_execution", "failed_infra", "failed_timeout"):
                    ps = "failed"
                else:
                    ps = "pending"
            else:
                ps = "pending"
            stage_progress.append({
                "step_id": sid,
                "label": wf_step.get("label", sid),
                "progress_state": ps,
                "has_gate": sid in gates and gates[sid].get("review") == "required",
            })

        needs_review = run.state == "paused"

        # Gate details for "waiting on you" band + role rails
        gate_info: dict | None = None
        if needs_review and run.current_step:
            gate_db_step = next(
                (s for s in (run.steps or []) if s.step_id == run.current_step), None
            )
            # Real curated artifacts from the engine's gate-card payload; stubs
            # only when the engine published nothing (legacy/demo runs).
            gate_payload = await _latest_step_payload(db, run.id, run.current_step)
            gate_files = _files_from_payload(gate_payload)
            if not gate_files and gate_db_step:
                gate_files = _stub_files_for_step(
                    run.current_step,
                    gate_db_step.exec_state,
                    gate_db_step.result_status,
                )
            gate_info = {
                "gate_step": run.current_step,
                "gate_label": next(
                    (w["label"] for w in wf_def if w["id"] == run.current_step),
                    run.current_step,
                ),
                "gate_status": gate_db_step.result_status if gate_db_step else None,
                "gate_file_count": len(gate_files),
                "gate_files": [f["label"] for f in gate_files],
                "gate_summary": str(gate_payload.get("summary") or "")[:400],
            }

        summary.update({
            "stage_progress": stage_progress,
            "total_stages": len(stage_progress),
            "needs_review": needs_review,
            "elapsed": _compute_elapsed(run.created_at),
            "workflow_name": run.workflow.name if run.workflow else "",
            "policy_name": run.policy.name if run.policy else "",
            "gate": gate_info,
        })
        results.append(summary)

    return results


@router.post("", status_code=201)
async def create_run(
    body: RunCreateRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Start a new pipeline run."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    if current_user.platform_role != "ADMIN":
        # Verify membership (platform ADMINs bypass)
        member_check = await db.execute(
            select(Membership).where(
                Membership.user_id == current_user.id,
                Membership.project_id == body.project_id,
            )
        )
        if member_check.scalar_one_or_none() is None:
            raise HTTPException(403, "You are not a member of this project")

    workflow = await db.get(Workflow, body.workflow_id)
    if workflow is None:
        raise HTTPException(404, f"Workflow {body.workflow_id} not found")

    is_adhoc = body.run_kind == "adhoc"
    if is_adhoc:
        # The query is the task and the branch is where it happens — both are
        # the whole point of the session, so missing ones fail here (422),
        # before the engine spends anything.
        if not body.query or not body.query.strip():
            raise HTTPException(422, "Ad-hoc runs require a query")
        if not body.source_branch:
            raise HTTPException(
                422, "Ad-hoc runs require a branch to work on (source_branch)")
        # The ad-hoc workflow is the single-step carrier for the query; anything
        # else would execute the query once per pipeline step.
        parsed_wf = _parse_workflow_yaml(workflow.yaml_content)
        if parsed_wf is None or len(parsed_wf.steps) != 1:
            raise HTTPException(
                400, "Ad-hoc runs require the 1-step 'adhoc' workflow "
                     "(this workflow has a different shape)")
    elif not body.story_id:
        raise HTTPException(422, "story_id is required for workflow runs")

    # Runs are project-scoped: platform templates (project_id IS NULL) must be
    # copied into the project first — they cannot back a run directly. The one
    # exception is the ad-hoc template, which is auto-provisioned into the
    # project on first use (ADR-016 "wide open": starting a session is the
    # same permission as starting a run).
    workflow_id = body.workflow_id
    if workflow.project_id is None or str(workflow.project_id) != body.project_id:
        if not (is_adhoc and workflow.project_id is None and workflow.name == "adhoc"):
            raise HTTPException(400, "Workflow does not belong to this project")
        # Auto-copy the platform ad-hoc template (workflow + policy + skills),
        # or reuse the project's existing copy so repeated submits are idempotent.
        project_wf = (
            await db.execute(
                select(Workflow).where(
                    Workflow.project_id == body.project_id,
                    Workflow.name == "adhoc",
                    Workflow.version == workflow.version,
                )
            )
        ).scalar_one_or_none()
        if project_wf is not None:
            workflow_id = str(project_wf.id)
        else:
            clone = Workflow(
                project_id=body.project_id,
                name=workflow.name,
                description=workflow.description,
                version=workflow.version,
                yaml_content=workflow.yaml_content,
                is_active=True,
                workflow_category_id=workflow.workflow_category_id,
            )
            db.add(clone)
            await db.flush()
            policies_result = await db.execute(
                select(Policy).where(Policy.workflow_id == workflow.id)
            )
            for pol in policies_result.scalars().all():
                db.add(Policy(
                    project_id=body.project_id,
                    workflow_id=clone.id,
                    name=pol.name,
                    version=pol.version,
                    yaml_content=pol.yaml_content,
                    is_active=pol.is_active,
                ))
            await clone_referenced_skills(
                db, workflow, body.project_id,
                store=getattr(request.app.state, "object_store", None),
            )
            workflow_id = str(clone.id)
            logger.info(
                "Ad-hoc template auto-provisioned: platform workflow %s → project %s clone %s",
                workflow.id, body.project_id, clone.id,
            )

    policy_id = body.policy_id
    if not policy_id:
        policies_result = await db.execute(
            select(Policy)
            .where(Policy.workflow_id == workflow_id, Policy.is_active == True)
            .order_by(Policy.created_at.desc())
            .limit(1)
        )
        policy = policies_result.scalar_one_or_none()
        if policy is None:
            raise HTTPException(400, "No active policy found for this workflow.")
        policy_id = str(policy.id)
    else:
        policy = await db.get(Policy, policy_id)
        if policy is None:
            raise HTTPException(404, f"Policy {policy_id} not found")
        if str(policy.workflow_id) != workflow_id:
            raise HTTPException(400, "Policy does not belong to the selected workflow")

    # ── Integrations: capture the user's verified selections ────────────────
    github = await _require_verified_integration(
        db, body.project_id, body.github_integration_id, {"github"}, "GitHub"
    )
    ai_vendor = await _require_verified_integration(
        db, body.project_id, body.ai_vendor_integration_id, AI_VENDOR_TYPES, "AI vendor"
    )
    jira = None
    if body.jira_integration_id:
        jira = await _require_verified_integration(
            db, body.project_id, body.jira_integration_id, {"jira"}, "Jira"
        )

    # Source branch: the user's per-run override wins; otherwise the selected
    # GitHub integration's base_branch (fallback "main"). The engine cuts the
    # run branch off it at init.
    github_config = github.config or {}
    source_branch = body.source_branch or str(github_config.get("base_branch") or "main")
    if body.source_branch:
        problem = _valid_source_branch(body.source_branch)
        if problem:
            raise HTTPException(422, f"Invalid source branch: {problem}")

    run = Run(
        project_id=_uuid.UUID(body.project_id),
        workflow_id=_uuid.UUID(workflow_id),
        policy_id=_uuid.UUID(policy_id),
        # Ad-hoc sessions carry no story — the query is the task. The branch
        # still rides source_branch (the engine verifies + adopts it at init).
        story_id=body.story_id if not is_adhoc else None,
        source_branch=source_branch,
        run_branch=None,
        github_integration_id=github.id,
        jira_integration_id=jira.id if jira else None,
        ai_vendor_integration_id=ai_vendor.id,
        started_by_user_id=current_user.id,
        state="pending",
        run_kind=body.run_kind,
        user_query=body.query.strip() if is_adhoc else None,
    )
    db.add(run)
    await db.flush()

    # Hand the run to the engine (ADR-003 work queue). Everything else —
    # branch creation, model resolution, env bundle — happens at engine init.
    db.add(WorkQueueItem(
        run_id=run.id,
        action="start",
        payload={"story_id": body.story_id},
    ))
    await db.commit()
    await db.refresh(run)

    logger.info(
        "Run created: %s project=%s kind=%s story=%s branch=%s github=%s ai=%s by=%s",
        run.id, body.project_id, run.run_kind, run.story_id, run.source_branch,
        github.id, ai_vendor.id, current_user.id,
    )
    return _run_summary(run)


@router.get("/{run_id}")
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Get full run detail with stage rail, artifact files, and gate info."""
    if enabled is None:
        raise HTTPException(401, "Authentication required")

    stmt = (
        select(Run)
        .options(
            selectinload(Run.steps),
            selectinload(Run.workflow),
            selectinload(Run.policy),
        )
        .where(Run.id == run_id)
    )
    result = await db.execute(stmt)
    run = result.scalars().first()
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    starter = None
    if run.started_by_user_id:
        starter = await db.get(User, run.started_by_user_id)
    return await _build_run_detail(db, run, started_by=starter)


@router.get("/{run_id}/file")
async def get_run_file(
    run_id: str,
    path: str = Query(..., description="File path within the step"),
    step_id: str | None = Query(None, description="Step whose commit to read at"),
    commit: str | None = Query(None, description="Exact commit (validated against run)"),
    request: Request = None,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Get file content for the output viewer. Returns the content and viewer type.

    Fallback chain (see platform_api/github_content.py): git at the step's
    recorded commit SHA (stage-accurate, when step_id/commit point at one) →
    demo stubs → placeholder. (ADR-014 removed the engine-side clone tree, so
    never-committed generated artifacts like ``changes.diff`` fall through to
    the stubs.)
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    # Git-at-sha first: the sha is derived server-side from the run's own
    # transition payloads — a caller-supplied `commit` is only honored when it
    # matches a recorded one (multi-visit loops have a sha per visit). Skipped
    # entirely when Secure Storage isn't wired (the chain must never 500).
    used_commit: str | None = None
    git_content: str | None = None
    secure_storage = getattr(request.app.state, "secure_storage", None)
    if step_id and secure_storage is not None:
        sha = await resolve_step_sha(db, run_id, step_id, commit)
        if sha:
            git_content = await git_fetch_content(
                db, run, sha, path, secure_storage)
            if git_content is not None:
                used_commit = sha

    content, source, resolved_path = build_chain(
        git_content, path, _STUB_FILE_CONTENT)

    # Determine viewer type from extension (single source of truth)
    viewer = _viewer_for_path(resolved_path)

    return {"path": resolved_path, "viewer": viewer, "content": content,
            "source": source, "commit": used_commit}


_LOG_FULL_MAX = 20 * 1024 * 1024    # cap on a full log served in one response
_LOG_TAIL_BYTES = 256 * 1024        # tail mode reads the last 256 KB, then keeps N lines
_LOG_TAIL_LINES_MAX = 2000
_LOG_RENDER_FULL_MAX = 2 * 1024 * 1024   # rendered full mode parses at most the last 2 MB

_RENDER_NOTICE = ("… earlier portion of the log omitted — the transcript "
                  "shows only the most recent portion")


def _decode_log(data: bytes) -> str:
    """Logs are UTF-8 text by convention — decode defensively (control
    characters from progress spinners must not kill the response)."""
    return data.decode("utf-8", "replace")


def _rendered_log_response(kind: str, mode: str, size: int,
                           window_cut: bool, parsed) -> dict:
    """Rendered agent-log response: typed blocks the UI renders as a
    transcript. A notice block leads when the view shows only a recent
    portion — the byte window / line limit cut the beginning, or the parser's
    block cap dropped it."""
    blocks = parsed.blocks
    truncated_render = window_cut or parsed.truncated
    if truncated_render:
        blocks = [LogBlock(kind="truncated_notice", text=_RENDER_NOTICE)] + blocks
    return {"kind": kind, "mode": mode, "size": size, "render": "agent",
            "truncated_render": truncated_render,
            "blocks": [b.to_dict() for b in blocks]}


def _raw_log_response(kind: str, mode: str, size: int,
                      window_cut: bool, text: str) -> dict:
    """Raw agent-log response: the served window exactly as the container
    wrote it (UTF-8 text, one event per line). The session page renders each
    line as a collapsed accordion row — no server-side parsing, no markdown,
    the transcript as it happened in the container."""
    return {"kind": kind, "mode": mode, "size": size, "render": "raw",
            "truncated_render": window_cut, "text": text}


def _pending_log_response(kind: str, mode: str) -> dict:
    """A launch-registered log whose first upload hasn't landed yet — the
    reference row exists (size 0) but the object does not. The live-tail UI
    treats this as "waiting", not an error, and keeps polling."""
    return {"kind": kind, "mode": mode, "size": 0, "pending": True,
            "content": "", "blocks": []}


@router.get("/{run_id}/logs")
async def get_run_logs(
    run_id: str,
    step_id: str = Query(..., description="Step id of the visit"),
    attempt_no: int = Query(..., description="Attempt number of the visit"),
    kind: str = Query(..., description="agent | container | diagnostics"),
    mode: str = Query("tail", pattern="^(tail|full)$"),
    lines: int = Query(200, ge=1, le=_LOG_TAIL_LINES_MAX),
    render: str | None = Query(None, pattern="^(agent|raw)$"),
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
    request: Request = None,
) -> dict:
    """Serve one attempt's log artifact from object storage (ADR-011).

    ``mode=tail`` reads only the last bytes from storage (ranged read — no
    full-object download) and returns the last `lines` lines; ``mode=full``
    returns the whole content up to a 20 MB cap (newest end kept when
    truncated). The object key ALWAYS comes from the run_logs reference — the
    store is never scanned and callers can never address arbitrary keys.

    ``render=agent`` (agent kind only) parses the served window into typed
    transcript blocks instead of raw text — assistant replies, tool calls,
    and the terminal result — capped at the last 2 MB so a 20 MB full log
    never turns one click into a megabyte parse. ``render=raw`` (agent kind
    only) returns the same window as plain text under ``text``, unparsed —
    the session page renders it line-by-line as collapsed event rows.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    if kind not in KINDS:
        raise HTTPException(400, f"unknown log kind {kind!r} (valid: {', '.join(KINDS)})")
    if render is not None and kind != "agent":
        raise HTTPException(400, "render= applies only to the agent log")
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")
    row = await db.scalar(
        select(RunLog).where(
            RunLog.run_id == run.id,
            RunLog.step_id == step_id,
            RunLog.attempt_no == attempt_no,
            RunLog.kind == kind))
    if row is None:
        raise HTTPException(
            404, f"no '{kind}' log for step '{step_id}' attempt {attempt_no}")
    store = getattr(request.app.state, "object_store", None)
    if store is None:
        raise HTTPException(503, "log storage not configured")

    if mode == "full":
        obj = await store.get(row.object_key)
        if obj is None:
            if row.size_bytes == 0:
                return _pending_log_response(kind, "full")
            raise HTTPException(404, "log artifact missing from storage (stale reference)")
        data = obj.data
        truncated = len(data) > _LOG_FULL_MAX
        if truncated:
            data = data[len(data) - _LOG_FULL_MAX:]    # keep the END — newest events
        if render is not None:
            text = _decode_log(data)
            window_cut = truncated or len(text) > _LOG_RENDER_FULL_MAX
            if len(text) > _LOG_RENDER_FULL_MAX:
                text = text[len(text) - _LOG_RENDER_FULL_MAX:]
            if render == "raw":
                return _raw_log_response(kind, "full", len(data),
                                         window_cut, text)
            return _rendered_log_response(kind, "full", len(data),
                                          window_cut, parse_agent_log(text))
        return {"kind": kind, "mode": "full", "size": len(data),
                "truncated": truncated, "content": _decode_log(data)}

    head = await store.head(row.object_key)
    if head is None:
        if row.size_bytes == 0:
            # Launch-registered row whose first upload hasn't landed yet
            # (agent.log heartbeats arrive every ~5 s) — pending, not a 404,
            # so the live-tail UI keeps polling instead of erroring.
            return _pending_log_response(kind, "tail")
        raise HTTPException(404, "log artifact missing from storage (stale reference)")
    start = max(0, head.size - _LOG_TAIL_BYTES)
    data = await store.get_range(row.object_key, start, head.size - 1)
    text = _decode_log(data)
    content = "\n".join(text.splitlines()[-lines:])
    if render is not None:
        # The byte window or the line limit may have cut the beginning —
        # either way the transcript starts partway in and says so.
        window_cut = head.size > _LOG_TAIL_BYTES or len(text.splitlines()) > lines
        if render == "raw":
            return _raw_log_response(kind, "tail", head.size,
                                     window_cut, content)
        return _rendered_log_response(kind, "tail", head.size,
                                      window_cut, parse_agent_log(content))
    return {"kind": kind, "mode": "tail", "size": head.size, "lines": lines,
            "content": content}


@router.get("/{run_id}/summary")
async def get_run_summary(
    run_id: str,
    step_id: str = Query(..., description="Step whose summary to return"),
    commit: str | None = Query(None, description="Visit-pinning commit sha (loops)"),
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """The FULL step summary, loaded on demand.

    Poll payloads carry only the 1500-char head (`summary`) so the timeline
    stays light; the complete text (`summary_full`) is stored in the
    completion transition's JSONB payload and fetched here when the reviewer
    clicks "Load full summary". Falls back to the truncated head for steps
    recorded before this feature existed (`truncated: true`).
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    payloads = await _summary_payloads(db, run_id, step_id)
    text, used_commit, truncated = _pick_full_summary(payloads, commit)
    if text is None:
        raise HTTPException(404, f"No summary recorded for step {step_id}")
    return {"run_id": run_id, "step_id": step_id, "commit": used_commit,
            "summary": text, "truncated": truncated}


@router.post("/{run_id}/decision")
async def submit_decision(
    run_id: str,
    body: DecisionRequest,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Queue a gate decision for the engine (ADR-003 dispatch model).

    The platform no longer mutates run state here: it validates that the run is
    actually paused at a gate, then enqueues a `continue` work item carrying the
    decision. The engine claims it and applies it — approve routes via the
    workflow's ``on:`` map, send_back resets later steps and replays with the
    reviewer comment as ``reviewer_feedback`` — and the UI's re-poll sees the
    new state. Concurrent decisions land as sibling items; the engine's
    supersede guard demotes stale claims.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    if run.state != "paused":
        raise HTTPException(
            409,
            f"Run {run_id} is in state '{run.state}', not paused — "
            "there is no open gate to decide",
        )

    # R5: an ad-hoc session's `paused` is awaiting_input, never an approval
    # gate — there is no gate card to decide. Turns and End session are the
    # only actions (POST /api/runs/{id}/turn, /end).
    if run.run_kind == "adhoc":
        raise HTTPException(
            409,
            "Ad-hoc sessions have no approval gates — use POST "
            f"/api/runs/{run_id}/turn to send a message or /end to finish",
        )

    actor = current_user.email or str(current_user.id)

    if body.action == "approve":
        payload = {"action": "approve", "comment": body.comment or "", "actor": actor}
        message = "Approval queued — the engine will advance the run."
    elif body.action == "send_back":
        if not body.send_back_to:
            raise HTTPException(400, "send_back_to is required for send_back action")

        # Validate the target stage exists in the workflow. Load the workflow by
        # id — accessing the `run.workflow` relationship here would lazy-load
        # outside a greenlet context in an async session.
        workflow = await db.get(Workflow, run.workflow_id)
        workflow_def = _parse_workflow_steps(workflow)
        valid_ids = {s["id"] for s in workflow_def}
        if body.send_back_to not in valid_ids:
            raise HTTPException(400, f"Unknown step '{body.send_back_to}'. Valid: {sorted(valid_ids)}")

        payload = {"action": "send_back", "send_back_to": body.send_back_to,
                   "comment": body.comment or "", "actor": actor}
        message = f"Send-back to '{body.send_back_to}' queued — the engine will rewind the run."
    else:
        raise HTTPException(400, f"Unknown action '{body.action}'. Use 'approve' or 'send_back'.")

    db.add(WorkQueueItem(run_id=run.id, action="continue", payload=payload))
    await db.commit()

    logger.info("Run %s decision %s queued by %s (comment: %s)",
                run_id, body.action, actor, (body.comment or "")[:80])
    return {"id": run_id, "decision": body.action, "message": message}


@router.post("/{run_id}/cancel", status_code=202)
async def cancel_run(
    run_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Stop a run (bookkeeper, ADR-003): enqueue a `cancel` dispatch token.

    The engine claims the item and stops the run — signalling its in-flight
    dispatch through an in-memory event (the step container is force-removed
    and the run transitions to `cancelled`), or, with no live dispatch (gate
    pause, still queued), voiding queued siblings and transitioning the run
    itself. The platform never mutates run state here.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")

    if run.state in TERMINAL_RUN_STATES:
        raise HTTPException(
            409,
            f"Run {run_id} already finished ({run.state}) — nothing to stop",
        )

    actor = current_user.email or str(current_user.id)
    db.add(WorkQueueItem(run_id=run.id, action="cancel", payload={"actor": actor}))
    await db.commit()

    logger.info("Run %s stop requested by %s", run_id, actor)
    return {"id": run_id, "message": "Stop queued — the engine will cancel the run."}


async def _session_dispatch_guard(run: Run, db: AsyncSession, what: str) -> None:
    """Shared turn/end preconditions (ADR-016 §2): the session must be ad-hoc,
    non-terminal, awaiting input, and not mid-reap — and must have no queued
    dispatch (one dispatch = one turn = one pause; a second would just be
    superseded by the engine)."""
    if run.run_kind != "adhoc":
        raise HTTPException(400, f"{what} applies to ad-hoc sessions only")
    if run.state in TERMINAL_RUN_STATES:
        raise HTTPException(409, f"Session {run.id} already finished ({run.state})")
    if run.state != "paused":
        raise HTTPException(
            409,
            f"Session {run.id} is in state '{run.state}' — wait for the "
            "current turn to finish first",
        )
    if run.session_phase == "reaping":
        raise HTTPException(
            409,
            "The session is ending after idle — retry in a moment (a fresh "
            "container will start for the next turn)",
        )
    pending = await db.scalar(
        select(WorkQueueItem.id)
        .where(WorkQueueItem.run_id == run.id,
               WorkQueueItem.state.in_(("pending", "claimed")))
        .limit(1))
    if pending is not None:
        raise HTTPException(409, "A dispatch is already queued for this session")


@router.post("/{run_id}/turn")
async def submit_turn(
    run_id: str,
    body: TurnRequest,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Queue the next session turn (bookkeeper, ADR-016 §2).

    One turn = one dispatch = one pause: the platform validates the session is
    awaiting input, then enqueues a `continue` item carrying the query. The
    engine claims it, writes the turn inbox, runs it in the live session
    container, and pauses the run again (awaiting_input) when the outbox
    answers. The platform never mutates run state here.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")
    await _session_dispatch_guard(run, db, "Turns")

    query = (body.query or "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    actor = current_user.email or str(current_user.id)
    db.add(WorkQueueItem(run_id=run.id, action="continue",
                         payload={"action": "turn", "query": query, "actor": actor}))
    await db.commit()

    logger.info("Run %s turn queued by %s (query: %s)", run_id, actor, query[:80])
    return {"id": run_id, "message": "Turn queued — the engine will run it."}


@router.post("/{run_id}/end")
async def end_session(
    run_id: str,
    db: AsyncSession = Depends(get_session),
    enabled: tuple[User, Identity] | None = Depends(get_current_enabled_user),
) -> dict:
    """Queue End session (bookkeeper, ADR-016 §2).

    The engine writes the end sentinel and waits for the container to
    commit+push+exit on its own (hard-kill fallback after the grace window),
    then completes the run. A session that was already reaped — no live
    container — completes directly: every turn committed as it happened, so
    nothing is lost.
    """
    if enabled is None:
        raise HTTPException(401, "Authentication required")
    current_user, _ = enabled

    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, f"Run {run_id} not found")
    await _session_dispatch_guard(run, db, "End session")

    actor = current_user.email or str(current_user.id)
    db.add(WorkQueueItem(run_id=run.id, action="continue",
                         payload={"action": "end", "actor": actor}))
    await db.commit()

    logger.info("Run %s end session queued by %s", run_id, actor)
    return {"id": run_id, "message": "End queued — the engine will close the session."}
