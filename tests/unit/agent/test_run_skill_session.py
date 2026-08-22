"""Unit — run_skill.sh session mode (ADR-016 Phase 2-3).

Host-run end-to-end test of the multi-turn coprocess loop, with a stub `claude`
stream-json server on PATH and an in-process HTTP server standing in for the
Object-Storage channels (presigned inbox GET / outbox PUT / transcript GET+PUT).
Binds 127.0.0.1:0 — no traffic leaves the test host, and no real model call
happens.

Covered: inbox seq matching (turn 1 arrives via the inbox exactly like later
turns), per-turn commit + outbox payloads (seq, response, commit, files,
per-turn cost delta), the `end` sentinel's clean exit with the final result,
the two claude-death failure modes (mid-turn EOF, idle death between turns),
and Phase 3 resume: a cold-start incarnation restores the transcript and
launches with --resume (+ --exclude-dynamic-system-prompt-sections), re-uploads
on graceful exit, and falls back to a fresh --session-id when the transcript
is missing.
"""

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from bheembhai.skill_publish import pack_skill

SCRIPT = Path(__file__).resolve().parents[3] / "agent" / "run_skill.sh"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True)


def _bundle(tmp_path: Path) -> tuple[Path, str]:
    """Real pack_skill output (deterministic tar.gz) + its sha256."""
    skill = SimpleNamespace(name="adhoc", files=[
        SimpleNamespace(path="SKILL.md", content="# bundled skill\n"),
    ])
    data = pack_skill(skill)
    bundle = tmp_path / "bundle-adhoc.tar.gz"
    bundle.write_bytes(data)
    return bundle, hashlib.sha256(data).hexdigest()


CLAUDE_STUB = r"""#!/usr/bin/env bash
# Fake claude for session-mode host tests: answers --help with the flags the
# runner feature-detects, then acts as a stream-json server — one result event
# per user message. Each query lands in answer.txt (work for commit_and_push);
# special queries DIE_MID_TURN / DIE_AFTER_ANSWER drive the failure tests.
# total_cost_usd is CUMULATIVE (0.01 preamble + 0.10 per turn), so the
# runner's per-turn delta accounting is exercised. The server invocation's
# full argv is logged to CLAUDE_ARGV_LOG so tests can assert the session /
# resume flags the runner passed (ADR-016 §3).
set -u
case "$*" in
  *--help*)
    echo "Usage: claude [options]
  -p, --print               Print response and exit
  --input-format <fmt>      Input format (text, stream-json)
  --output-format <fmt>     Output format (text, json, stream-json)
  --verbose                 Enable verbose logging
  --model <model>
  --mcp-config <path>       Load MCP servers from a JSON config
  --strict-mcp-config
  --dangerously-skip-permissions
  --permission-mode <mode>
  --session-id <id>         Start a session with the given id
  --resume <id>             Resume the session with the given id
  --exclude-dynamic-system-prompt-sections"
    exit 0 ;;
  *--version*) echo "2.1.218 (stub)"; exit 0 ;;
esac
printf '%s' "$*" > "${CLAUDE_ARGV_LOG:-/dev/null}" 2>/dev/null || true
n=0
while IFS= read -r line; do
  q=$(printf '%s' "$line" | jq -r '.message.content[0].text // ""' 2>/dev/null)
  case "$q" in
    "") continue ;;
    *"ad-hoc agent session"*)
      printf '{"type":"result","subtype":"success","result":"ready for turns","total_cost_usd":0.01,"modelUsage":{"claude-stub":{"costUSD":0.01}}}\n'
      ;;
    "DIE_MID_TURN") exit 1 ;;
    *)
      n=$((n + 1))
      printf '%s' "$q" > answer.txt
      total=$(awk -v n="$n" 'BEGIN{printf "%.2f", 0.01 + n * 0.10}')
      printf '{"type":"result","subtype":"success","result":"echo: %s","total_cost_usd":%s,"modelUsage":{"claude-stub":{"costUSD":0.10}}}\n' "$q" "$total"
      [ "$q" = "DIE_AFTER_ANSWER" ] && exit 0
      ;;
  esac
done
"""


class _SessionChannelHandler(BaseHTTPRequestHandler):
    def do_GET(self):   # the inbox / transcript restore (engine -> container)
        if self.path == "/transcript":
            if self.server.transcript is None:
                self.send_response(404)
                self.end_headers()
                return
            body = self.server.transcript
        else:
            inbox = self.server.inbox
            if inbox is None:
                self.send_response(404)
                self.end_headers()
                return
            body = inbox
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_PUT(self):   # the outbox / transcript upload (container -> engine)
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/transcript":
            self.server.transcripts.append(body)
        else:
            self.server.outboxes.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):   # silence per-request logging
        pass


class _SessionChannels:
    """In-process stand-in for the Object-Storage turn channels (ADR-016 §2)."""

    def __init__(self):
        self.outboxes: list[bytes] = []
        self.transcripts: list[bytes] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0),
                                           _SessionChannelHandler)
        self._server.inbox = None
        self._server.outboxes = self.outboxes
        self._server.transcript = None
        self._server.transcripts = self.transcripts
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def set_inbox(self, payload: dict):
        self._server.inbox = json.dumps(payload).encode()

    def set_transcript(self, payload: bytes):
        self._server.transcript = payload


SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _start_session(tmp_path: Path, channels: _SessionChannels, *,
                   resume: bool = False):
    """Launches the real script in session mode (copy git mode: the workspace
    IS the repo) with the stub claude on PATH. resume=True makes this the
    reaper's cold-start incarnation: BB_SESSION_RESUME=1 + a transcript GET
    presign (set the served content via channels.set_transcript first).
    Returns (proc, ws, out)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    _git("init", "-q", "-b", "main", cwd=ws)
    _git("config", "user.email", "test@bheembhai.local", cwd=ws)
    _git("config", "user.name", "test", cwd=ws)
    (ws / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=ws)
    _git("commit", "-qm", "seed", cwd=ws)

    out = tmp_path / "out"
    out.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(CLAUDE_STUB)
    (bin_dir / "claude").chmod(0o755)

    bundle, sha = _bundle(tmp_path)
    ctx_file = tmp_path / "context.json"
    ctx_file.write_text(json.dumps({
        "user_query": "first query",
        "allowed_result_statuses": ["completed"],
        "result_status_meanings": {},
        "gate_follows": False,
        "story_id": "",
    }))

    base = dict(os.environ)
    # Credential + channel hygiene: never inherit ambient presigns or tokens.
    for k in ("BB_RESULT_PUT_URL", "BB_PROGRESS_PUT_URL", "BB_LOG_PUT_URL",
              "BB_DIAG_PUT_URL", "GH_TOKEN", "JIRA_URL", "JIRA_USERNAME",
              "JIRA_API_TOKEN", "JIRA_EMAIL", "BB_SESSION_ID",
              "BB_SESSION_RESUME", "BB_TRANSCRIPT_PUT_URL",
              "BB_TRANSCRIPT_GET_URL"):
        base.pop(k, None)
    home = tmp_path / "home"   # claude's ~/.claude/projects transcript dir
    home.mkdir()
    env = {**base,
           "BB_GIT_MODE": "0",
           "SKILL": "adhoc",
           "RUN_BRANCH": "feat/session",
           "RESULT_DIR": str(out),
           "WORKSPACE_DIR": str(ws),
           "BB_SESSION": "1",
           "BB_SESSION_ID": SESSION_ID,
           "BB_SESSION_RESUME": "1" if resume else "0",
           "BB_TRANSCRIPT_PUT_URL": f"{channels.base}/transcript",
           "BB_MODE": "adhoc",
           "BB_SKILL_URL": bundle.as_uri(),
           "BB_SKILL_SHA256": sha,
           "CONTEXT_FILE": str(ctx_file),
           "BB_INBOX_GET_URL": f"{channels.base}/inbox",
           "BB_OUTBOX_PUT_URL": f"{channels.base}/outbox",
           "HOME": str(home),
           "CLAUDE_ARGV_LOG": str(tmp_path / "argv.txt"),
           "PATH": f"{bin_dir}:{base.get('PATH', '')}"}
    if resume:
        env["BB_TRANSCRIPT_GET_URL"] = f"{channels.base}/transcript"
    proc = subprocess.Popen(["bash", str(SCRIPT)], env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    return proc, ws, out


def _wait_outbox(channels: _SessionChannels, seq: int, timeout: float = 30) -> dict:
    """Waits for an outbox whose seq >= `seq` — NOT merely any outbox: the list
    keeps earlier turns, so "any" would return turn 1's payload instantly and
    let the test race ahead of the container's 2s inbox poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if channels.outboxes:
            latest = json.loads(channels.outboxes[-1])
            if latest.get("seq", 0) >= seq:
                return latest
        time.sleep(0.2)
    raise AssertionError(f"no outbox with seq >= {seq} landed within timeout")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Reap a session proc a failing assert left running — its poll loop would
    otherwise outlive the test and hold the stdio pipes (and orphan a claude
    stub). No-op once the proc has already exited."""
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _wait_proc(proc: subprocess.Popen, timeout: float = 40) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise


def _proc_output(proc: subprocess.Popen) -> str:
    out, err = proc.communicate()
    return f"stdout:\n{out[-2000:]}\nstderr:\n{err[-2000:]}"


def _result(out: Path) -> dict:
    return json.loads((out / "bb_step_result.json").read_text())


def test_session_two_turns_then_end_sentinel(tmp_path):
    with _SessionChannels() as channels:
        proc, ws, out = _start_session(tmp_path, channels)
        try:
            # Turn 1 arrives via the inbox, exactly like every later turn.
            channels.set_inbox({"seq": 1, "kind": "turn",
                                "query": "add a hello file"})
            o1 = _wait_outbox(channels, 1)
            assert o1["seq"] == 1
            assert "echo: add a hello file" in o1["response"]
            assert o1["commit"] is not None and len(o1["commit"]) >= 7
            assert {"status": "A", "path": "answer.txt"} in o1["files"]
            assert abs(o1["cost_usd"] - 0.10) < 1e-9     # per-turn DELTA, not total
            assert o1["cost_reported"] is True

            channels.set_inbox({"seq": 2, "kind": "turn",
                                "query": "improve the file"})
            o2 = _wait_outbox(channels, 2)
            assert o2["seq"] == 2
            assert "echo: improve the file" in o2["response"]
            assert {"status": "M", "path": "answer.txt"} in o2["files"]
            assert abs(o2["cost_usd"] - 0.10) < 1e-9

            # The end sentinel: commit+push once more and exit cleanly.
            channels.set_inbox({"seq": 3, "kind": "end"})
            rc = _wait_proc(proc)
            assert rc == 0, _proc_output(proc)

            payload = _result(out)
            assert payload["status"] == "completed"
            assert "session ended" in payload["reason"]
            assert abs(payload["cost_usd"] - 0.21) < 1e-6   # cumulative total

            # Per-turn durability: one commit per file-changing turn (seed + 2).
            count = _git("rev-list", "--count", "HEAD", cwd=ws).stdout.strip()
            assert count == "3"
            # The transcript log carries both turns' stream-json result lines.
            agent_log = (out / "agent.log").read_text()
            assert "=== session started" in agent_log
            assert "echo: add a hello file" in agent_log
            assert "echo: improve the file" in agent_log
            # Fresh launch: the engine-minted session id rides the CLI argv so
            # the transcript filename is deterministic (ADR-016 §3).
            argv = (tmp_path / "argv.txt").read_text()
            assert f"--session-id {SESSION_ID}" in argv
            assert "--resume" not in argv
        finally:
            _kill_tree(proc)


def test_session_end_sentinel_with_zero_turns(tmp_path):
    """Reap-before-turn-1: the end sentinel alone must close the session with
    a clean completed verdict and no outbox writes."""
    with _SessionChannels() as channels:
        proc, _ws, out = _start_session(tmp_path, channels)
        try:
            channels.set_inbox({"seq": 1, "kind": "end"})
            rc = _wait_proc(proc)
            assert rc == 0, _proc_output(proc)
            payload = _result(out)
            assert payload["status"] == "completed"
            assert "session ended" in payload["reason"]
            assert channels.outboxes == []
        finally:
            _kill_tree(proc)


def test_session_resume_restores_transcript(tmp_path):
    """Reap-then-resume (ADR-016 §3): a cold-start incarnation downloads the
    prior session's transcript to claude's on-disk path and relaunches with
    --resume + the dynamic-prompt exclusion; its graceful exit re-uploads the
    transcript so the NEXT incarnation can resume again."""
    seed = b'{"type":"user","message":{"role":"user"}}\n'
    with _SessionChannels() as channels:
        channels.set_transcript(seed)   # the prior incarnation's upload
        proc, ws, out = _start_session(tmp_path, channels, resume=True)
        try:
            channels.set_inbox({"seq": 1, "kind": "turn",
                                "query": "continue the session"})
            o1 = _wait_outbox(channels, 1)
            assert o1["seq"] == 1
            assert "echo: continue the session" in o1["response"]

            # The restore landed the file exactly where --resume looks for it:
            # $HOME/.claude/projects/<munged-cwd>/<session-id>.jsonl. The
            # slash after projects/ is load-bearing: the munged cwd starts
            # with a leading dash (the leading / munged), so concatenating
            # without it collapses projects/ + -path into projects-path.
            tf = Path(
                f"{tmp_path}/home/.claude/projects/"
                f"{str(ws).replace('/', '-')}/{SESSION_ID}.jsonl")
            assert tf.read_bytes() == seed

            channels.set_inbox({"seq": 2, "kind": "end"})
            rc = _wait_proc(proc)
            assert rc == 0, _proc_output(proc)

            argv = (tmp_path / "argv.txt").read_text()
            assert f"--resume {SESSION_ID}" in argv
            assert "--exclude-dynamic-system-prompt-sections" in argv
            assert "--session-id" not in argv
            # Graceful exit round-trips the transcript channel for the next
            # incarnation.
            assert channels.transcripts and channels.transcripts[-1] == seed
            assert _result(out)["status"] == "completed"
        finally:
            _kill_tree(proc)


def test_session_resume_without_transcript_falls_back_to_fresh(tmp_path):
    """A resume incarnation whose transcript is missing (the first incarnation
    died before claude saved one) must still serve turns — on a fresh
    --session-id session."""
    with _SessionChannels() as channels:
        proc, _ws, out = _start_session(tmp_path, channels, resume=True)
        try:
            channels.set_inbox({"seq": 1, "kind": "turn", "query": "first work"})
            o1 = _wait_outbox(channels, 1)
            assert o1["seq"] == 1
            assert "echo: first work" in o1["response"]
            channels.set_inbox({"seq": 2, "kind": "end"})
            rc = _wait_proc(proc)
            assert rc == 0, _proc_output(proc)
            assert _result(out)["status"] == "completed"
            argv = (tmp_path / "argv.txt").read_text()
            assert f"--session-id {SESSION_ID}" in argv
            assert "--resume" not in argv
        finally:
            _kill_tree(proc)


def test_session_claude_death_mid_turn_fails_execution(tmp_path):
    """EOF without a result event means the process died mid-turn — the runner
    must fail the turn (the engine re-delivers it on a fresh container)."""
    with _SessionChannels() as channels:
        proc, _ws, out = _start_session(tmp_path, channels)
        try:
            channels.set_inbox({"seq": 1, "kind": "turn",
                                "query": "DIE_MID_TURN"})
            rc = _wait_proc(proc)
            assert rc == 1, _proc_output(proc)
            payload = _result(out)
            assert payload["status"] == "failed_execution"
            assert "mid-turn" in payload["reason"]
            assert channels.outboxes == []   # no outbox: the turn never completed
        finally:
            _kill_tree(proc)


def test_session_claude_death_while_idle_fails_execution(tmp_path):
    """A turn can complete and land its outbox, and claude can still die
    between turns — the idle check must surface that as failed_execution."""
    with _SessionChannels() as channels:
        proc, _ws, out = _start_session(tmp_path, channels)
        try:
            channels.set_inbox({"seq": 1, "kind": "turn",
                                "query": "DIE_AFTER_ANSWER"})
            o1 = _wait_outbox(channels, 1)
            assert o1["seq"] == 1
            rc = _wait_proc(proc)
            assert rc == 1, _proc_output(proc)
            payload = _result(out)
            assert payload["status"] == "failed_execution"
            assert "exited while awaiting input" in payload["reason"]
        finally:
            _kill_tree(proc)
