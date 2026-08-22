"""agent.log → typed blocks for the rendered transcript view.

agent.log is the raw stdout+stderr of ``claude -p --output-format stream-json
--verbose`` tee'd into a file (``agent/run_skill.sh``), so it is line-delimited
JSON (JSONL) stream events interleaved with plain-text wrapper lines (git
clone/push messages, the ``=== running <skill> ===`` banner, CLI stderr).

This module converts that mess into ordered, typed blocks the UI can render as
a readable transcript: assistant replies, tool calls/results, the terminal
result event, and the plain wrapper noise — assistant text as Markdown, tool
activity as collapsed rows.

The format is hostile to strict parsing, so everything here is tolerant:

- the file is heartbeat-uploaded WHILE it is being appended, so the served
  content can end mid-line / mid-JSON;
- the tail window can start mid-event (orphaned ``tool_result`` first);
- the CLI version is not pinned, so unknown event types and missing fields
  must never raise.

A line that cannot be parsed as JSON (or parses to an unknown type) is simply
skipped — never an error, never rendered. This module is pure and sync: no
I/O, no DB, no storage — unit-testable in isolation.
"""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

BLOCK_KINDS = (
    "assistant_text",
    "tool_call",
    "tool_result",
    "result_text",
    "plain",
    "truncated_notice",
)
TOOL_STATUS = ("ok", "error", "unknown")

_RENDER_BLOCKS_MAX = 5000        # hard cap on returned blocks (drop from FRONT)
_TOOL_RAW_MAX = 32 * 1024        # per-block raw input/output cap (file dumps)
_TOOL_SUMMARY_MAX = 160          # one-line tool input summary
_TOOL_OUTPUT_SUMMARY_MAX = 400   # one-line tool output summary
_TRUNCATION_MARKER = "\n…[truncated]"

# Preferred input key per tool name — the field that names what the call does,
# so the collapsed row reads like a command history instead of a JSON blob.
_TOOL_INPUT_KEYS = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Task": "description",
    "Skill": "skill",
}

_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Drop terminal escape sequences (progress spinners, colors) from text."""
    if "\x1b" not in text:
        return text
    return _ANSI_CSI_RE.sub("", text).replace("\x1b", "")


def _collapse(text: str, max_chars: int) -> str:
    """Single-line summary: whitespace runs (incl. newlines) → one space,
    then truncate with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) > max_chars:
        return collapsed[:max_chars] + "…"
    return collapsed


def _cap(text: str, max_chars: int) -> str:
    """Display-only raw payload capped at max_chars, with an honesty marker."""
    if len(text) > max_chars:
        return text[:max_chars] + _TRUNCATION_MARKER
    return text


def summarize_tool_input(name: str | None, data: object) -> str:
    """One-line summary of a tool_use input: the preferred field for the known
    tool name, else the first string value, else compact JSON. Empty string
    when there is no input to describe."""
    if isinstance(data, dict):
        key = _TOOL_INPUT_KEYS.get(name) if name else None
        if key is not None:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _collapse(value, _TOOL_SUMMARY_MAX)
        for value in data.values():
            if isinstance(value, str) and value.strip():
                return _collapse(value, _TOOL_SUMMARY_MAX)
        if data:
            return _collapse(
                json.dumps(data, ensure_ascii=False), _TOOL_SUMMARY_MAX)
        return ""
    if isinstance(data, str):
        return _collapse(data, _TOOL_SUMMARY_MAX) if data.strip() else ""
    if data is None:
        return ""
    return _collapse(json.dumps(data, ensure_ascii=False), _TOOL_SUMMARY_MAX)


def _extract_tool_output(content: object) -> tuple[str, str]:
    """(summary, full) of a tool_result's content — a plain string or a list
    of content blocks (only ``type=="text"`` blocks carry readable text)."""
    if isinstance(content, str):
        full = content
    elif isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        full = "\n".join(parts)
    else:
        full = ""
    full = strip_ansi(full)
    return _collapse(full, _TOOL_OUTPUT_SUMMARY_MAX), full


def summarize_tool_output(content: object) -> str:
    """One-line summary of a tool_result's content."""
    return _extract_tool_output(content)[0]


@dataclass
class LogBlock:
    """One renderable unit of the transcript. ``kind`` selects the UI row
    style; only the fields that kind uses are populated (``to_dict`` drops
    the rest so payloads stay small)."""

    kind: str
    text: str | None = None            # assistant_text / result_text / plain / notice
    tool_name: str | None = None       # tool_call
    tool_id: str | None = None         # tool_call + tool_result pairing key
    tool_summary: str | None = None    # tool_call input summary
    tool_status: str | None = None     # tool_result: ok | error
    raw: str | None = None             # expandable full input / output (capped)
    error: bool = False                # result_text from a failed (is_error) run

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind}
        for attr in ("text", "tool_name", "tool_id", "tool_summary",
                     "tool_status", "raw"):
            value = getattr(self, attr)
            if value is not None:
                d[attr] = value
        if self.error:
            d["error"] = True
        return d


@dataclass
class LogParseResult:
    blocks: list[LogBlock] = field(default_factory=list)
    truncated: bool = False     # block cap dropped the FRONT (oldest events)
    events_seen: int = 0        # JSON lines with a recognized event type
    lines_skipped: int = 0      # malformed-JSON / unknown-type lines dropped


def _content_items(event: dict) -> list[dict]:
    """message.content as a list of blocks — tolerant of a missing message, a
    non-dict message, a bare string, and a single dict (older/other CLI
    shapes)."""
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        return [c for c in content if isinstance(c, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _norm(text: str) -> str:
    """Whitespace-normalized form for result-event dedup (line-wrapping in
    the two sources must not read as a difference)."""
    return " ".join(text.split())


def _last_assistant_text(blocks: deque[LogBlock]) -> str | None:
    """Text of the newest assistant_text block still in the window — the
    anchor for result-event dedup. Scanning the tail keeps the anchor honest
    even after the front-drop cap removed older blocks."""
    for block in reversed(blocks):
        if block.kind == "assistant_text" and block.text is not None:
            return block.text
    return None


def _emit(blocks: deque[LogBlock], block: LogBlock,
          max_blocks: int, result: LogParseResult) -> None:
    """Append a block, dropping from the FRONT past the cap (the newest events
    are what a tail-oriented log view shows — same convention as the raw log
    endpoint's end-kept truncation)."""
    blocks.append(block)
    if len(blocks) > max_blocks:
        blocks.popleft()
        result.truncated = True


def parse_agent_log(text: str, *,
                    max_blocks: int = _RENDER_BLOCKS_MAX) -> LogParseResult:
    """Parse agent.log content into ordered render blocks.

    Tolerant by construction: non-JSON lines become ``plain`` blocks
    (consecutive ones coalesced), JSON lines that fail to parse or carry an
    unknown event type are skipped, and every field access assumes absence.
    Never raises for malformed input.
    """
    result = LogParseResult()
    blocks: deque[LogBlock] = deque()
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    pending_plain: list[str] = []

    def flush_plain() -> None:
        nonlocal pending_plain
        if pending_plain:
            _emit(blocks, LogBlock(kind="plain", text="\n".join(pending_plain)),
                  max_blocks, result)
            pending_plain = []

    for raw_line in normalized.splitlines():
        if not raw_line.strip():
            continue
        try:
            parsed = json.loads(raw_line)
        except (ValueError, TypeError):
            # Not JSON. A line starting like an event fragment (the tail
            # window or a heartbeat upload cut a long JSON line in half) is
            # skipped; anything else is wrapper noise → a plain block.
            if strip_ansi(raw_line).lstrip()[:1] in ('{', '"', '}', ']'):
                result.lines_skipped += 1
                continue
            pending_plain.append(strip_ansi(raw_line))
            continue
        flush_plain()
        if not isinstance(parsed, dict):
            result.lines_skipped += 1
            continue
        event = parsed
        etype = event.get("type")
        if etype == "assistant":
            result.events_seen += 1
            for item in _content_items(event):
                if item.get("type") == "text":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        _emit(blocks, LogBlock(
                            kind="assistant_text", text=strip_ansi(text)),
                            max_blocks, result)
                elif item.get("type") == "tool_use":
                    tool_name = item.get("name")
                    raw_input = item.get("input")
                    raw = None
                    if raw_input is not None:
                        try:
                            raw = _cap(json.dumps(
                                raw_input, ensure_ascii=False),
                                _TOOL_RAW_MAX)
                        except (TypeError, ValueError):
                            raw = None
                    _emit(blocks, LogBlock(
                        kind="tool_call",
                        tool_name=tool_name if isinstance(tool_name, str)
                        else None,
                        tool_id=(item.get("id")
                                 if isinstance(item.get("id"), str)
                                 else None),
                        tool_summary=summarize_tool_input(
                            tool_name if isinstance(tool_name, str)
                            else None, raw_input),
                        raw=raw),
                        max_blocks, result)
        elif etype == "user":
            result.events_seen += 1
            for item in _content_items(event):
                if item.get("type") != "tool_result":
                    continue  # prompt echo and side channels — noise
                output_summary, full = _extract_tool_output(
                    item.get("content"))
                _emit(blocks, LogBlock(
                    kind="tool_result",
                    tool_id=(item.get("tool_use_id")
                             if isinstance(item.get("tool_use_id"), str)
                             else None),
                    tool_status=("error" if item.get("is_error")
                                 else "ok"),
                    tool_summary=output_summary or None,
                    raw=_cap(full, _TOOL_RAW_MAX) if full else None),
                    max_blocks, result)
        elif etype == "result":
            result.events_seen += 1
            res_text = event.get("result")
            if not isinstance(res_text, str) or not res_text.strip():
                # An error result may carry only an error payload — its
                # message is still the transcript's last word.
                err = event.get("error")
                if isinstance(err, dict) and isinstance(
                        err.get("message"), str):
                    res_text = err["message"]
                else:
                    continue
            anchor = _last_assistant_text(blocks)
            if anchor is not None and _norm(res_text) == _norm(anchor):
                # The terminal event echoes the final assistant message,
                # which the transcript already rendered mid-stream —
                # emitting it again would duplicate the reply.
                continue
            _emit(blocks, LogBlock(
                kind="result_text", text=strip_ansi(res_text),
                error=bool(event.get("is_error"))),
                max_blocks, result)
        else:
            # system init, stream_event, future CLI types — not part of
            # the readable transcript.
            result.lines_skipped += 1
    flush_plain()
    result.blocks = list(blocks)
    return result
