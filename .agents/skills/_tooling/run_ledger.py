#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER_PATH = PROJECT_ROOT / ".local" / "beembhai" / "run-ledger.jsonl"
LEDGER_ENV_VARS = ("BEEMBHAI_RUN_LEDGER_PATH", "BEEBHAI_RUN_LEDGER_PATH")
KNOWN_SKILLS = (
    "prd",
    "prd-decompose",
    "user-story",
    "tech-design",
    "project-scaffold",
    "story-design",
    "test-creator",
    "implement",
    "test-verify",
    "code-review",
    "design-sync",
    "pr-create",
    "story-implement",
    "epic-sequence",
    "revert-run",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def configured_ledger_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for env_name in LEDGER_ENV_VARS:
        env_value = os.getenv(env_name)
        if env_value:
            return Path(env_value).expanduser()
    return DEFAULT_LEDGER_PATH


def sanitize_segment(value: str) -> str:
    cleaned = []
    for char in value.lower():
        if char.isalnum() or char in {"-", "_"}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    segment = "".join(cleaned).strip("-_")
    return segment or "run"


def generate_run_id(skill_name: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{sanitize_segment(skill_name)}-{timestamp}-{uuid4().hex[:8]}"


def load_entries(ledger_path: Path) -> list[dict[str, Any]]:
    if not ledger_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    with ledger_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - defensive
                raise ValueError(f"invalid JSON on line {line_number} in {ledger_path}") from exc
            payload["_line_number"] = line_number
            entries.append(payload)
    return entries


def append_entry(ledger_path: Path, entry: dict[str, Any]) -> None:
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True))
        handle.write("\n")


def parse_key_value(values: list[str], *, field_name: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_value in values:
        if "=" not in raw_value:
            raise ValueError(f"{field_name} values must use KEY=VALUE format: {raw_value}")
        key, value = raw_value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{field_name} keys must not be empty")
        parsed[key] = value
    return parsed


def load_json_stdin() -> dict[str, Any]:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    return json.loads(raw)


def infer_story_key(*texts: str | None) -> str | None:
    pattern = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
    for text in texts:
        if not text:
            continue
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def infer_skill_name(*texts: str | None) -> str | None:
    for text in texts:
        if not text:
            continue
        for skill_name in KNOWN_SKILLS:
            if skill_name in text:
                return skill_name
    return None


def build_entry(args: argparse.Namespace, run_id: str) -> dict[str, Any]:
    changed_files = list(dict.fromkeys(args.changed_file or []))
    commits = list(dict.fromkeys(args.commit or []))
    external_actions = list(dict.fromkeys(args.external_action or []))
    entry: dict[str, Any] = {
        "run_id": run_id,
        "event": args.event,
        "story_key": args.story_key,
        "skill_name": args.skill_name,
        "timestamp": utc_now(),
        "status": args.status,
        "branch": args.branch,
        "step": args.step,
        "parent_run_id": args.parent_run_id,
        "supersedes_run_id": args.supersedes_run_id,
        "changed_files": changed_files,
        "commits": commits,
        "external_actions": external_actions,
        "reversible": args.reversible,
        "notes": args.notes,
        "metadata": parse_key_value(args.field or [], field_name="field"),
    }
    return {key: value for key, value in entry.items() if value not in (None, [], {}, "")}


def build_hook_entry(payload: dict[str, Any]) -> dict[str, Any]:
    hook_event_name = payload.get("hook_event_name") or "unknown"
    turn_id = payload.get("turn_id")
    session_id = payload.get("session_id")
    prompt = payload.get("prompt")
    last_assistant_message = payload.get("last_assistant_message")
    transcript_path = payload.get("transcript_path")
    skill_name = infer_skill_name(prompt, last_assistant_message, transcript_path, hook_event_name)
    story_key = infer_story_key(prompt, last_assistant_message, transcript_path)
    run_id = turn_id or session_id or generate_run_id(hook_event_name)
    source_text = payload.get("source") or payload.get("tool_name") or hook_event_name
    tool_name = payload.get("tool_name")
    metadata = {
        key: value
        for key, value in payload.items()
        if key not in {"hook_event_name", "turn_id", "session_id"}
    }
    entry: dict[str, Any] = {
        "run_id": run_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "event": hook_event_name.lower(),
        "skill_name": skill_name or hook_event_name.lower(),
        "story_key": story_key or "unknown",
        "timestamp": utc_now(),
        "status": hook_event_name.lower(),
        "branch": None,
        "step": tool_name or source_text,
        "changed_files": [],
        "commits": [],
        "external_actions": [],
        "reversible": "unknown",
        "metadata": metadata,
    }
    return {key: value for key, value in entry.items() if value not in (None, [], {}, "")}


def run_matches(entry: dict[str, Any], args: argparse.Namespace) -> bool:
    filters = {
        "run_id": args.run_id,
        "story_key": args.story_key,
        "skill_name": args.skill_name,
        "branch": args.branch,
        "status": args.status,
        "event": args.event,
    }
    for key, expected in filters.items():
        if expected is not None and entry.get(key) != expected:
            return False
    return True


def group_runs(
    entries: list[dict[str, Any]], args: argparse.Namespace
) -> OrderedDict[str, list[dict[str, Any]]]:
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for entry in entries:
        if not run_matches(entry, args):
            continue
        run_id = entry["run_id"]
        groups.setdefault(run_id, []).append(entry)
    return groups


def resolve_latest_run(entries: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    if args.run_id:
        matching_entries = [entry for entry in entries if entry.get("run_id") == args.run_id]
        if matching_entries and all(run_matches(entry, args) for entry in matching_entries):
            return {"run_id": args.run_id, "entries": matching_entries}
        raise SystemExit(f"run_id {args.run_id} not found")

    groups = group_runs(entries, args)
    if not groups:
        raise SystemExit("no matching run found")

    latest_run_id = next(reversed(groups))
    return {"run_id": latest_run_id, "entries": groups[latest_run_id]}


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def summarise_entries(run_id: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    first = entries[0]
    last = entries[-1]
    changed_files = unique_preserving_order(
        [path for entry in entries for path in entry.get("changed_files", [])]
    )
    commits = unique_preserving_order(
        [commit for entry in entries for commit in entry.get("commits", [])]
    )
    external_actions = unique_preserving_order(
        [action for entry in entries for action in entry.get("external_actions", [])]
    )
    statuses = unique_preserving_order(
        [status for status in (entry.get("status") for entry in entries) if status is not None]
    )
    notes = next((entry.get("notes") for entry in reversed(entries) if entry.get("notes")), None)
    return {
        "run_id": run_id,
        "story_key": first.get("story_key"),
        "skill_name": first.get("skill_name"),
        "branch": first.get("branch"),
        "parent_run_id": first.get("parent_run_id"),
        "supersedes_run_id": first.get("supersedes_run_id"),
        "event_count": len(entries),
        "first_timestamp": first.get("timestamp"),
        "last_timestamp": last.get("timestamp"),
        "changed_files": changed_files,
        "commits": commits,
        "external_actions": external_actions,
        "statuses": statuses,
        "reversible": first.get("reversible"),
        "notes": notes,
        "metadata": first.get("metadata", {}),
        "entries": entries,
    }


def render_preview(summary: dict[str, Any]) -> str:
    lines = [
        f"Target run: {summary['run_id']}",
        f"Story: {summary.get('story_key') or '-'}",
        f"Skill: {summary.get('skill_name') or '-'}",
        f"Branch: {summary.get('branch') or '-'}",
        f"Events: {summary['event_count']}",
        f"First event: {summary.get('first_timestamp') or '-'}",
        f"Last event: {summary.get('last_timestamp') or '-'}",
        f"Reversible: {summary.get('reversible') or '-'}",
    ]
    if summary["changed_files"]:
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in summary["changed_files"])
    if summary["commits"]:
        lines.append("Commits:")
        lines.extend(f"- {commit}" for commit in summary["commits"])
    if summary["external_actions"]:
        lines.append("External actions:")
        lines.extend(f"- {action}" for action in summary["external_actions"])
    if summary["notes"]:
        lines.append(f"Notes: {summary['notes']}")
    return "\n".join(lines)


def cmd_record(args: argparse.Namespace) -> int:
    ledger_path = configured_ledger_path(args.ledger_path)
    run_id = args.run_id or generate_run_id(args.skill_name)
    entry = build_entry(args, run_id)
    append_entry(ledger_path, entry)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "ledger_path": str(ledger_path),
                "entry": entry,
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    ledger_path = configured_ledger_path(args.ledger_path)
    payload = load_json_stdin()
    if not payload:
        return 0
    entry = build_hook_entry(payload)
    append_entry(ledger_path, entry)
    return 0


def cmd_latest(args: argparse.Namespace) -> int:
    ledger_path = configured_ledger_path(args.ledger_path)
    entries = load_entries(ledger_path)
    resolved = resolve_latest_run(entries, args)
    summary = summarise_entries(resolved["run_id"], resolved["entries"])
    if args.json:
        payload = {
            "ledger_path": str(ledger_path),
            "run_id": summary["run_id"],
            "summary": summary,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(summary["run_id"])
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    ledger_path = configured_ledger_path(args.ledger_path)
    entries = load_entries(ledger_path)
    resolved = resolve_latest_run(entries, args)
    summary = summarise_entries(resolved["run_id"], resolved["entries"])
    if args.json:
        payload = {"ledger_path": str(ledger_path), "summary": summary}
        print(json.dumps(payload, sort_keys=True))
        return 0
    print(render_preview(summary))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Shared run ledger hook")
    parser.add_argument("--ledger-path", dest="ledger_path", help="Override the ledger path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Append a run event to the ledger")
    record.add_argument("--run-id", dest="run_id")
    record.add_argument("--event", required=True)
    record.add_argument("--story-key", required=True)
    record.add_argument("--skill-name", required=True)
    record.add_argument("--branch")
    record.add_argument("--step")
    record.add_argument("--status")
    record.add_argument("--parent-run-id", dest="parent_run_id")
    record.add_argument("--supersedes-run-id", dest="supersedes_run_id")
    record.add_argument("--changed-file", action="append", dest="changed_file")
    record.add_argument("--commit", action="append")
    record.add_argument("--external-action", action="append", dest="external_action")
    record.add_argument(
        "--reversible",
        choices=["yes", "no", "unknown"],
        default="unknown",
    )
    record.add_argument("--notes")
    record.add_argument("--field", action="append")
    record.set_defaults(func=cmd_record)

    hook = subparsers.add_parser("hook", help="Append a Codex hook event to the ledger")
    hook.set_defaults(func=cmd_hook)

    latest = subparsers.add_parser("latest", help="Print the latest matching run id")
    latest.add_argument("--run-id", dest="run_id")
    latest.add_argument("--story-key")
    latest.add_argument("--skill-name")
    latest.add_argument("--branch")
    latest.add_argument("--status")
    latest.add_argument("--event")
    latest.add_argument("--json", action="store_true")
    latest.set_defaults(func=cmd_latest)

    preview = subparsers.add_parser("preview", help="Show a human-readable revert preview")
    preview.add_argument("--run-id", dest="run_id")
    preview.add_argument("--story-key")
    preview.add_argument("--skill-name")
    preview.add_argument("--branch")
    preview.add_argument("--status")
    preview.add_argument("--event")
    preview.add_argument("--json", action="store_true")
    preview.set_defaults(func=cmd_preview)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
