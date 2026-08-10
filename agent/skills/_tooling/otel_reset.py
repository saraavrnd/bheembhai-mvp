#!/usr/bin/env python3
"""
otel_reset.py — clean-slate the OTEL cost pipeline before a story-implement run.

Run as the FIRST precondition action of story-implement. It establishes a clean cost
boundary so the captured cost reflects THIS story's work, not the generic pre-story
discussion (which story to pick, AC review, off-topic chat) that happened earlier in
the session.

What it does (in order):
  1. Kill any running otel_receiver.py (frees the port, stops accumulation).
  2. Archive the existing ~/.claude/otel-costs.json to a timestamped backup, then remove
     it so the receiver starts from an empty store (no stale/pre-fix data carried over).
  3. Start a fresh otel_receiver.py in the background, stamped with the current
     RECEIVER_VERSION so future logic changes auto-invalidate old data.

WHY archive instead of hard-delete: recoverable if you ever need to inspect prior data;
the effect on the next run is identical (receiver starts empty either way).

WHY this is safe to do mid-session: the intervals emitted BEFORE this reset are the
pre-story discussion you don't want in the story's cost. Dropping them is noise removal,
not data loss. Capture from this point forward is predominantly the story.

NOTE: capture is "story work from here forward" — long unrelated tangents mid-story will
still land in the number. Keep mid-story detours short for the cleanest figure.

Usage:
  python otel_reset.py                 # kill + archive + restart (default)
  python otel_reset.py --no-restart    # kill + archive only (receiver started elsewhere)
  python otel_reset.py --port 4318     # override receiver port
Exit: 0 on success (receiver up, or --no-restart done), 1 on failure to start.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

RECEIVER_VERSION = 2  # bump when receiver aggregation logic changes → old data auto-invalidated
CLAUDE_ROOT = os.environ.get("CLAUDE_ROOT", os.path.expanduser("~/.claude"))
OUT_FILE = os.path.join(CLAUDE_ROOT, "otel-costs.json")
_HERE = os.path.dirname(os.path.abspath(__file__))
RECEIVER = os.path.join(_HERE, "otel_receiver.py")


def _find_receiver_pids(port):
    """Return PIDs of running otel_receiver.py (matched by script name and/or port)."""
    pids = set()
    # by script name
    try:
        out = subprocess.run(["pgrep", "-f", "otel_receiver.py"], capture_output=True, text=True)
        pids.update(int(p) for p in out.stdout.split() if p.strip().isdigit())
    except Exception:
        pass
    # by port holder (belt-and-suspenders; lsof may not be present)
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
        pids.update(int(p) for p in out.stdout.split() if p.strip().isdigit())
    except Exception:
        pass
    # never kill ourselves
    pids.discard(os.getpid())
    return pids


def kill_receiver(port):
    pids = _find_receiver_pids(port)
    if not pids:
        print("[otel_reset] no running receiver found.")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[otel_reset] sent SIGTERM to receiver pid {pid}.")
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"[otel_reset] could not kill pid {pid}: {e}", file=sys.stderr)
    # give them a moment to release the port
    time.sleep(1.0)
    # escalate if still alive
    for pid in _find_receiver_pids(port):
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"[otel_reset] SIGKILL to stubborn receiver pid {pid}.")
        except Exception:
            pass


def archive_costs():
    if not os.path.exists(OUT_FILE):
        print("[otel_reset] no existing otel-costs.json to archive.")
        return
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(CLAUDE_ROOT, f"otel-costs.{ts}.bak.json")
    try:
        os.replace(OUT_FILE, backup)
        print(f"[otel_reset] archived old costs → {backup}")
    except Exception as e:
        # fall back to unlink so the receiver still starts clean
        print(f"[otel_reset] archive failed ({e}); removing instead.", file=sys.stderr)
        try:
            os.remove(OUT_FILE)
        except Exception:
            pass


def _seed_version_stamp():
    """Write a fresh store carrying the receiver version, so downstream tooling can
    detect and ignore data from an older (buggy) receiver."""
    try:
        os.makedirs(CLAUDE_ROOT, exist_ok=True)
        json.dump(
            {
                "__meta": {
                    "receiver_version": RECEIVER_VERSION,
                    "reset_at": datetime.now().isoformat(),
                }
            },
            open(OUT_FILE, "w"),
            indent=2,
        )
    except Exception as e:
        print(f"[otel_reset] warn: could not seed version stamp: {e}", file=sys.stderr)


def start_receiver(port):
    if not os.path.exists(RECEIVER):
        print(f"[otel_reset] ERROR: receiver not found at {RECEIVER}", file=sys.stderr)
        return False
    env = dict(os.environ, OTEL_RECEIVER_PORT=str(port))
    # detached background process; stdout/stderr to a log next to the receiver
    log = open(os.path.join(CLAUDE_ROOT, "otel_receiver.log"), "a")
    try:
        subprocess.Popen(
            [sys.executable, RECEIVER], stdout=log, stderr=log, env=env, start_new_session=True
        )
    except Exception as e:
        print(f"[otel_reset] ERROR starting receiver: {e}", file=sys.stderr)
        return False
    # wait for the port to come up
    for _ in range(20):
        time.sleep(0.25)
        if _find_receiver_pids(port):
            print(f"[otel_reset] receiver started on port {port} (version {RECEIVER_VERSION}).")
            return True
    print(
        "[otel_reset] WARNING: receiver process launched but not confirmed on port.",
        file=sys.stderr,
    )
    return True  # launched; may just be slow to bind


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=int(os.environ.get("OTEL_RECEIVER_PORT", 4318)))
    ap.add_argument(
        "--no-restart", action="store_true", help="kill + archive only; don't start a new receiver"
    )
    args = ap.parse_args()

    print("[otel_reset] clean-slating OTEL cost pipeline for a fresh story boundary…")
    kill_receiver(args.port)  # 1. stop old receiver
    archive_costs()  # 2. archive/remove stale costs
    if args.no_restart:
        _seed_version_stamp()
        print("[otel_reset] done (no restart requested).")
        return 0
    _seed_version_stamp()  # stamp BEFORE start so the receiver loads a clean, versioned store
    ok = start_receiver(args.port)  # 3. start fresh receiver
    if not ok:
        print(
            "[otel_reset] FAILED to start receiver — story-implement should note that "
            "cost capture is unavailable and post a Jira comment instead of a cost value.",
            file=sys.stderr,
        )
        return 1
    print("[otel_reset] clean boundary established. Capture from here reflects THIS story.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
