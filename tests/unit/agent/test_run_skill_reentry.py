"""Unit — run_skill.sh re-entry guard (git INIT block).

Regression for the implement re-loop failure on run 03ad1cd6: code-review returned
``changes_requested``, the workflow routed back to ``implement``, and the engine
relaunched the step into the SAME workspace dir (clones/<run>/implement/1) whose
``repo/`` clone from the first visit was still there. The INIT block cloned into the
non-empty dir, git failed instantly ("destination path 'repo' already exists"), the
fallback clone of the source branch failed the same way, and the script exited 3
(failed_init). The guard drops the leftover so re-entry resumes from the last
pushed state.

Runs the REAL script with BB_STOP_AFTER_INIT=1 (exits right after git init, before
Claude Code) against a local bare repo — no Docker, no network.
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "agent" / "run_skill.sh"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=True)


def _make_remote(tmp_path: Path) -> Path:
    """Local bare repo with one commit on main — stands in for GitHub."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "main", cwd=seed)
    _git("config", "user.email", "test@bheembhai.local", cwd=seed)
    _git("config", "user.name", "test", cwd=seed)
    (seed / "README.md").write_text("seed\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "seed", cwd=seed)
    remote = tmp_path / "remote.git"
    _git("clone", "-q", "--bare", str(seed), str(remote), cwd=tmp_path)
    return remote


def _run_skill(tmp_path: Path, remote: Path, run_branch: str, workspace: Path | None = None):
    ws = workspace if workspace is not None else tmp_path / "ws"
    out = tmp_path / "out"
    ws.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "BB_GIT_MODE": "1",
           "GIT_REMOTE_URL": str(remote),
           "GIT_SOURCE_BRANCH": "main",
           "RUN_BRANCH": run_branch,
           "SKILL": "test",
           "RESULT_DIR": str(out),
           "WORKSPACE_DIR": str(ws),
           "BB_STOP_AFTER_INIT": "1"}
    env.pop("GH_TOKEN", None)  # local clones need no token
    proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=60)
    return proc, ws, out


def _current_branch(repo: Path) -> str:
    return subprocess.run(["git", "branch", "--show-current"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()


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
