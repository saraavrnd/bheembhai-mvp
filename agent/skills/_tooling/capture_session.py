#!/usr/bin/env python3
"""
capture_session.py — Claude Code Stop hook.

Fired by Claude Code at the end of every turn. Writes the session_id and
transcript_path (received via stdin from the Claude Code hook runtime) to
.claude/last-session.json in the project root.

story_tokens.py reads that file to locate the JSONL without --session-id
or path-searching. The file is always current for the active session and is
safe to overwrite on every turn.

Output: nothing (exit 0 always — this hook must never block).
"""
import json
import os
import sys


def main():
    try:
        data = json.load(sys.stdin)
        session_id = data.get("session_id", "")
        transcript_path = data.get("transcript_path", "")

        if session_id and transcript_path and os.path.exists(transcript_path):
            out_path = os.path.join(os.getcwd(), ".claude", "last-session.json")
            with open(out_path, "w") as f:
                json.dump({"session_id": session_id, "transcript_path": transcript_path}, f)
    except Exception:
        pass  # never surface errors — this is a best-effort capture

    sys.exit(0)


if __name__ == "__main__":
    main()
