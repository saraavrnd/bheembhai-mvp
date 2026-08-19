"""Unit — run_skill.sh re-entry guard (git INIT block) + S3 skill download.

Re-entry regression for the implement re-loop failure on run 03ad1cd6:
code-review returned ``changes_requested``, the workflow routed back to
``implement``, and the engine relaunched the step into the SAME workspace dir
(clones/<run>/implement/1) whose ``repo/`` clone from the first visit was still
there. The INIT block cloned into the non-empty dir, git failed instantly
("destination path 'repo' already exists"), the fallback clone of the source
branch failed the same way, and the script exited 3 (failed_init). The guard
drops the leftover so re-entry resumes from the last pushed state.

The skill-delivery tests below stop at BB_STOP_AFTER_SKILLS=1 — right after the
download block (Phase 1: BB_SKILL_URL curl + sha256 verify + extract), before
Claude Code — against a bundle built by the REAL pack_skill (file:// URL, no
network). BB_STOP_AFTER_INIT must be 0 or the script exits too early.
"""

import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from bheembhai.skill_publish import pack_skill

SCRIPT = Path(__file__).resolve().parents[3] / "agent" / "run_skill.sh"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True)


def _make_remote(tmp_path: Path, extra_files: dict[str, str] | None = None) -> Path:
    """Local bare repo with one commit on main — stands in for GitHub."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "test@bheembhai.local", cwd=seed)
    _git("config", "user.name", "test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    for relpath, content in (extra_files or {}).items():
        p = seed / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "seed", cwd=seed)
    remote = tmp_path / "remote.git"
    _git("clone", "-q", "--bare", str(seed), str(remote), cwd=tmp_path)
    return remote


def _run_skill(tmp_path: Path, remote: Path, run_branch: str, workspace: Path | None = None,
               env_overrides: dict[str, str] | None = None):
    ws = workspace if workspace is not None else tmp_path / "ws"
    out = tmp_path / "out"
    ws.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    base = dict(os.environ)
    # The PUT-URL channels are per-test: never inherit host/ambient presigns.
    for k in ("BB_RESULT_PUT_URL", "BB_PROGRESS_PUT_URL",
              "BB_LOG_PUT_URL", "BB_DIAG_PUT_URL"):
        base.pop(k, None)
    env = {**base,
           "BB_GIT_MODE": "1",
           "GIT_REMOTE_URL": str(remote),
           "GIT_SOURCE_BRANCH": "main",
           "RUN_BRANCH": run_branch,
           "SKILL": "test",
           "RESULT_DIR": str(out),
           "WORKSPACE_DIR": str(ws),
           "BB_STOP_AFTER_INIT": "1",
           **(env_overrides or {})}
    env.pop("GH_TOKEN", None)  # local clones need no token
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    return proc, ws, out


def _current_branch(repo: Path) -> str:
    return subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_reentry_drops_leftover_and_clones_pushed_branch(tmp_path):
    remote = _make_remote(tmp_path)
    # First visit: branch doesn't exist on the remote -> created from main.
    proc1, ws, out = _run_skill(tmp_path, remote, "feat/reentry")
    assert proc1.returncode == 0, proc1.stderr
    assert (ws / "repo" / ".git").is_dir()
    # Push, as the COMMIT block would, so the branch exists for the next visit.
    _git("push", "-q", "origin", "feat/reentry", cwd=ws / "repo")
    # Leftover from the first visit — the exact state the re-loop collided with.
    (ws / "repo" / "LEFT_BEHIND.txt").write_text("stale")
    # Second visit reuses the same workspace dir.
    proc2, ws, out = _run_skill(tmp_path, remote, "feat/reentry", workspace=ws)
    assert proc2.returncode == 0, proc2.stderr
    assert "cloned existing run branch feat/reentry" in (out / "agent.log").read_text()
    # The guard replaced the leftover, not re-used it: the stale marker is gone.
    assert not (ws / "repo" / "LEFT_BEHIND.txt").exists()
    assert _current_branch(ws / "repo") == "feat/reentry"


def test_reentry_with_branch_not_yet_on_remote_repairs_too(tmp_path):
    remote = _make_remote(tmp_path)
    # First visit creates the branch locally (step-1 crash before push scenario).
    proc1, ws, out = _run_skill(tmp_path, remote, "feat/never-pushed")
    assert proc1.returncode == 0, proc1.stderr
    # Second visit: leftover repo present AND the branch is not on the remote —
    # the guard must drop the leftover so the source-branch fallback can run.
    proc2, ws, out = _run_skill(tmp_path, remote, "feat/never-pushed", workspace=ws)
    assert proc2.returncode == 0, proc2.stderr
    assert "created run branch feat/never-pushed from main" in (out / "agent.log").read_text()
    assert _current_branch(ws / "repo") == "feat/never-pushed"


# ── Skill delivery: BB_SKILL_URL download (Phase 1) ──────────────────────────


def _bundle(tmp_path: Path, files: dict[str, str] | None = None,
            name: str = "test") -> tuple[Path, str]:
    """Real pack_skill output (deterministic tar.gz) + its sha256."""
    skill = SimpleNamespace(name=name, files=[
        SimpleNamespace(path=p, content=c)
        for p, c in (files or {"SKILL.md": "# bundled skill\n"}).items()
    ])
    data = pack_skill(skill)
    bundle = tmp_path / f"bundle-{name}.tar.gz"
    bundle.write_bytes(data)
    return bundle, hashlib.sha256(data).hexdigest()


def _skills_stop_env(url: str | None = None, sha: str | None = None) -> dict[str, str]:
    env = {"BB_STOP_AFTER_INIT": "0", "BB_STOP_AFTER_SKILLS": "1"}
    if url is not None:
        env["BB_SKILL_URL"] = url
    if sha is not None:
        env["BB_SKILL_SHA256"] = sha
    return env


def _result(out: Path) -> dict:
    return json.loads((out / "bb_step_result.json").read_text())


def test_download_delivers_real_content_not_a_symlink(tmp_path):
    # Repo tracks nothing at .claude: the OLD default was a symlink to the
    # baked /skills library. Phase 1 must deliver the bundle as real files.
    remote = _make_remote(tmp_path)
    bundle, sha = _bundle(tmp_path)
    proc, ws, _ = _run_skill(tmp_path, remote, "feat/download",
                             env_overrides=_skills_stop_env(bundle.as_uri(), sha))
    assert proc.returncode == 0, proc.stderr
    skills = ws / "repo" / ".claude" / "skills"
    assert skills.is_dir() and not skills.is_symlink()
    assert (skills / "test" / "SKILL.md").read_text() == "# bundled skill\n"


def test_download_overwrites_repo_tracked_skills(tmp_path):
    remote = _make_remote(tmp_path, {".claude/skills/tracked.md": "tracked\n"})
    bundle, sha = _bundle(tmp_path)
    proc, ws, _ = _run_skill(tmp_path, remote, "feat/download-tracked",
                             env_overrides=_skills_stop_env(bundle.as_uri(), sha))
    assert proc.returncode == 0, proc.stderr
    skills = ws / "repo" / ".claude" / "skills"
    assert (skills / "test" / "SKILL.md").read_text() == "# bundled skill\n"
    # The tracked file was force-replaced: no longer at that path.
    assert not (skills / "tracked.md").exists()
    # The worktree sees the removal (the COMMIT block restores it later, after
    # this hook — see run_skill.sh hygiene block).
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ws / "repo",
                            capture_output=True, text=True, check=True).stdout
    assert ".claude/skills/tracked.md" in status


def test_download_sha_mismatch_fails_infra_without_extracting(tmp_path):
    remote = _make_remote(tmp_path)
    bundle, _ = _bundle(tmp_path)
    proc, ws, out = _run_skill(tmp_path, remote, "feat/download-badsha",
                               env_overrides=_skills_stop_env(bundle.as_uri(), "0" * 64))
    assert proc.returncode == 4, proc.stderr
    payload = _result(out)
    assert payload["status"] == "failed_infra"
    assert "sha256" in payload["reason"]
    # Refused BEFORE extraction: nothing landed in the worktree.
    assert not (ws / "repo" / ".claude" / "skills" / "test").exists()


def test_download_missing_url_fails_init(tmp_path):
    remote = _make_remote(tmp_path)
    proc, _ws, out = _run_skill(tmp_path, remote, "feat/download-nourl",
                                env_overrides=_skills_stop_env(None))
    assert proc.returncode == 2, proc.stderr
    payload = _result(out)
    assert payload["status"] == "failed_init"
    assert "BB_SKILL_URL" in payload["reason"]


def test_download_unreachable_url_fails_infra(tmp_path):
    remote = _make_remote(tmp_path)
    dead = tmp_path / "gone" / "skill.tar.gz"   # never created
    proc, ws, out = _run_skill(tmp_path, remote, "feat/download-dead",
                               env_overrides=_skills_stop_env(dead.as_uri(), "0" * 64))
    assert proc.returncode == 4, proc.stderr
    payload = _result(out)
    assert payload["status"] == "failed_infra"
    assert "download failed" in payload["reason"]
    assert not (ws / "repo" / ".claude" / "skills" / "test").exists()


# ── ADR-014 upload channels: presigned PUTs ─────────────────────────────────


class _PutHandler(BaseHTTPRequestHandler):
    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.capture.append((self.path, body,
                                    self.headers.get("Content-Type")))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):   # silence per-request logging
        pass


class _PutCapture:
    """In-process HTTP server capturing the agent's presigned-PUT uploads
    (ADR-014). Binds 127.0.0.1:0 — no traffic leaves the test host."""

    def __init__(self):
        self.record: list[tuple[str, bytes, str | None]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _PutHandler)
        self._server.capture = self.record
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def urls(self) -> dict[str, str]:
        base = f"http://127.0.0.1:{self._server.server_address[1]}"
        return {"BB_RESULT_PUT_URL": f"{base}/result",
                "BB_PROGRESS_PUT_URL": f"{base}/progress",
                "BB_LOG_PUT_URL": f"{base}/log",
                "BB_DIAG_PUT_URL": f"{base}/diag"}

    def puts_for(self, path: str) -> list[tuple[str, bytes, str | None]]:
        return [t for t in self.record if t[0] == path]


def _mock_env(bundle: Path, sha: str, put_urls: dict[str, str] | None = None) -> dict[str, str]:
    """Full-path mock run (no Claude Code): skills delivered, BB_MOCK emits the
    completed verdict, the EXIT trap ships the channels."""
    env = {"BB_STOP_AFTER_INIT": "0", "BB_STOP_AFTER_SKILLS": "0",
           "BB_SKILL_URL": bundle.as_uri(), "BB_SKILL_SHA256": sha,
           "BB_MOCK": "1", "BB_MOCK_SECONDS": "0"}
    env.update(put_urls or {})
    return env


def test_stop_after_skills_uploads_heartbeat_channels_from_trap(tmp_path):
    """BB_STOP_AFTER_SKILLS exits before the heartbeat loop ever starts — the
    EXIT trap alone must ship the final progress + agent.log (best-effort) and
    skip the result (no verdict was emitted yet)."""
    remote = _make_remote(tmp_path)
    bundle, sha = _bundle(tmp_path)
    with _PutCapture() as cap:
        proc, _ws, _out = _run_skill(
            tmp_path, remote, "feat/put-stop",
            env_overrides={**_skills_stop_env(bundle.as_uri(), sha),
                           **cap.urls()})
    assert proc.returncode == 0, proc.stderr
    assert len(cap.puts_for("/progress")) >= 1
    assert len(cap.puts_for("/log")) >= 1
    assert cap.puts_for("/result") == []


def test_mock_run_uploads_result_and_logs(tmp_path):
    """Full mock path: the result PUT lands at exit with the completed verdict;
    agent.log and diagnostics.txt ride along."""
    remote = _make_remote(tmp_path)
    bundle, sha = _bundle(tmp_path)
    with _PutCapture() as cap:
        proc, _ws, _out = _run_skill(
            tmp_path, remote, "feat/put-mock",
            env_overrides=_mock_env(bundle, sha, cap.urls()))
    assert proc.returncode == 0, proc.stderr
    results = cap.puts_for("/result")
    assert len(results) == 1
    _, body, ctype = results[0]
    assert ctype == "application/json"
    assert json.loads(body)["status"] == "completed"
    assert len(cap.puts_for("/diag")) >= 1
    assert len(cap.puts_for("/log")) >= 1


def test_unreachable_result_url_exits_failed_infra(tmp_path):
    """The result channel is CRITICAL: a dead PUT URL degrades the exit code to
    4 and rewrites the local payload to failed_infra so the engine retries the
    step (the push landed — the retry is idempotent)."""
    remote = _make_remote(tmp_path)
    bundle, sha = _bundle(tmp_path)
    proc, _ws, out = _run_skill(
        tmp_path, remote, "feat/put-dead",
        env_overrides=_mock_env(bundle, sha,
                                {"BB_RESULT_PUT_URL": "http://127.0.0.1:1/result"}))
    assert proc.returncode == 4, proc.stderr
    payload = _result(out)
    assert payload["status"] == "failed_infra"
    assert "result upload failed" in payload["reason"]
