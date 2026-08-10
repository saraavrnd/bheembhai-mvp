"""BheemBhai MVP — FastAPI backend with SSE streaming for the demo UI."""
import json, os, queue, re, shutil
from pathlib import Path


def _load_dotenv(path=None):
    """Load KEY=value lines from a .env file into os.environ, WITHOUT overriding variables
    already set in the real environment.

    Precedence (highest first): real environment > .env file > code defaults. This means a
    secret injected by AWS/systemd, or a one-off `FOO=bar uvicorn ...`, always wins over the
    file — the .env is the convenience default layer, not an override. Runs before `import
    engine` because engine reads BB_WORKDIR at import time.

    Location: $BB_ENV_FILE if set, else ./.env next to this file. Missing file = no-op.
    Format: KEY=value per line; # comments and blank lines ignored; surrounding quotes and an
    optional leading `export ` are stripped. Values are taken verbatim (no shell expansion).
    """
    p = Path(path or os.environ.get("BB_ENV_FILE", Path(__file__).parent / ".env"))
    if not p.is_file():
        return []
    loaded = []
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # strip a trailing inline comment only when the value isn't quoted
        if v and v[0] not in "\"'" and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:      # real env wins — don't override
            os.environ[k] = v
            loaded.append(k)
    return loaded


_DOTENV_LOADED = _load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import engine
from engine import BUS, RUNS, DockerRuntime, Policy, Run, Workflow, WORKDIR

HERE = Path(__file__).parent
CONFIG = HERE / "config"
app = FastAPI(title="BheemBhai")
engine.init_db()
if _DOTENV_LOADED:
    engine.log.info("loaded %d vars from .env: %s", len(_DOTENV_LOADED),
                    ", ".join(sorted(_DOTENV_LOADED)))

# Validate env configuration once, at boot — so a malformed BB_ALLOWED_MODELS or a missing
# GIT_REMOTE_URL surfaces immediately with a clear message, not per-request later.
_cfg_problems = engine.check_startup_config()
if _cfg_problems:
    for _p in _cfg_problems:
        engine.log.error("STARTUP CONFIG: %s", _p)
    if os.environ.get("BB_STRICT_STARTUP", "0") == "1":
        raise RuntimeError("startup config invalid:\n  - " + "\n  - ".join(_cfg_problems))
    engine.log.error("continuing despite config problems (set BB_STRICT_STARTUP=1 to refuse boot)")
else:
    engine.log.info("startup config OK")


class StartRun(BaseModel):
    workflow: str = "story-delivery"
    policy: str = "strict"
    story_id: str = ""
    source_branch: str = ""   # base to cut the run branch from; blank -> GIT_SOURCE_BRANCH env


class Decision(BaseModel):
    decision: str = "approve"
    comment: str = ""


def _runtime():
    return DockerRuntime(image=os.environ.get("BB_AGENT_IMAGE", "bheembhai/agent:latest"),
                         endpoint=os.environ.get("BB_DOCKER_ENDPOINT") or None)


def _installed_skills():
    """Skill ids the platform ships, for workflow validation. Best-effort: the skills live
    in the agent image, but the local agent/skills dir mirrors them at build time. If we
    can't enumerate them, return None so the skill-existence check is skipped rather than
    wrongly failing a valid workflow.
    """
    d = Path(os.environ.get("BB_SKILLS_DIR", HERE / "agent" / "skills"))
    if not d.is_dir():
        return None
    names = {p.name for p in d.iterdir() if p.is_dir()}
    return names or None


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/config")
def list_config():
    return {"workflows": sorted(p.stem.replace("workflow-", "") for p in CONFIG.glob("workflow-*.yaml")),
            "policies": sorted(p.stem.replace("policy-", "") for p in CONFIG.glob("policy-*.yaml")),
            "image": os.environ.get("BB_AGENT_IMAGE", "bheembhai/agent:latest"),
            "git_mode": os.environ.get("BB_GIT_MODE", "1") != "0",
            "source_branch": os.environ.get("GIT_SOURCE_BRANCH", "main"),
            "has_remote": bool(os.environ.get("GIT_REMOTE_URL")),
            "model_profile": os.environ.get("BB_MODEL_PROFILE", "anthropic")}


def _fresh_workspace():
    ws = WORKDIR / "workspaces" / os.urandom(4).hex()
    ws.mkdir(parents=True, exist_ok=True)
    seed = os.environ.get("BB_SEED_REPO")
    if seed and Path(seed).exists():
        shutil.copytree(seed, ws, dirs_exist_ok=True)
    # The agent container runs non-root; make the workspace writable by it.
    for root, dirs, files in os.walk(ws):
        for d in dirs:
            try: os.chmod(os.path.join(root, d), 0o777)
            except Exception: pass
        for f in files:
            try: os.chmod(os.path.join(root, f), 0o666)
            except Exception: pass
    try: os.chmod(ws, 0o777)
    except Exception: pass
    return ws


@app.post("/api/runs")
def start_run(body: StartRun):
    wf_path = CONFIG / f"workflow-{body.workflow}.yaml"
    pol_path = CONFIG / f"policy-{body.policy}.yaml"
    if not wf_path.exists(): raise HTTPException(404, f"no workflow '{body.workflow}'")
    if not pol_path.exists(): raise HTTPException(404, f"no policy '{body.policy}'")
    wf = Workflow.load(wf_path)
    pol = Policy.load(pol_path)
    try:
        engine.validate_workflow(wf, known_skills=_installed_skills())
        engine.validate_pairing(wf, pol)
    except (engine.WorkflowError, engine.PairingError) as e:
        raise HTTPException(422, str(e))
    # --- Git mode (default) vs copy mode (BB_GIT_MODE=0) -------------------------------
    # Git mode: work happens on a real branch cut from the source branch. The run branch is
    # auto-derived feat/<story>/<DDMMYYYYHHmm> — unique per run, owned exclusively by it, so
    # steps (which run sequentially) never conflict. Copy mode is the older BB_SEED_REPO path,
    # kept as a fallback for demos without a remote.
    git_mode = os.environ.get("BB_GIT_MODE", "1") != "0"
    git_url = os.environ.get("GIT_REMOTE_URL", "")
    run_branch = source_branch = None
    if git_mode:
        if not git_url:
            raise HTTPException(400, "GIT_REMOTE_URL is not set (or set BB_GIT_MODE=0 for copy mode)")
        if not (body.story_id or "").strip():
            raise HTTPException(400, "a story id is required in git mode (it names the run branch)")
        source_branch = (body.source_branch or "").strip() or os.environ.get("GIT_SOURCE_BRANCH", "main")
        from datetime import datetime
        stamp = datetime.now().strftime("%d%m%Y%H%M")
        safe_story = re.sub(r"[^A-Za-z0-9._-]", "-", body.story_id.strip())
        run_branch = f"feat/{safe_story}/{stamp}"

    run = Run(wf, pol, _runtime(),
              repo=(None if git_mode else _fresh_workspace()),
              story_id=(body.story_id or None),
              git=({"url": git_url, "source_branch": source_branch, "run_branch": run_branch}
                   if git_mode else None))
    RUNS[run.id] = run
    run.start()
    return {"run_id": run.id, "workflow": run.wf.name, "policy": run.policy.name,
            "story_id": run.story_id,
            "run_branch": run_branch, "source_branch": source_branch}


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, body: Decision):
    run = RUNS.get(run_id)
    if not run: raise HTTPException(404, "unknown run")
    if body.decision not in ("approve", "request_changes"):
        raise HTTPException(400, "decision must be approve or request_changes")
    run.approve(body.decision, comment=body.comment)
    return {"ok": True, "decision": body.decision}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    conn = engine.db()
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    steps = conn.execute("SELECT * FROM steps WHERE run_id=? ORDER BY started_at", (run_id,)).fetchall()
    conn.close()
    if not r: raise HTTPException(404, "unknown run")
    return {"run": dict(r), "steps": [dict(s) for s in steps]}


@app.get("/api/runs/{run_id}/file")
def read_artifact(run_id: str, path: str):
    """Read one artifact from a run's workspace, so a reviewer can open what the skill wrote.

    Path traversal is the obvious risk here: `path` comes from the client. Resolve it and
    require that it stays inside the run's own workspace.
    """
    ws = engine.WORKSPACES.get(run_id)
    if not ws:
        raise HTTPException(404, "unknown run")
    root = Path(ws).resolve()
    try:
        target = (root / path).resolve()
        target.relative_to(root)          # raises if it escaped the workspace
    except (ValueError, OSError):
        raise HTTPException(400, "invalid path")
    if not target.is_file():
        # Be specific: a file can be listed by git yet absent on the host (written inside
        # the container to a path the mount doesn't hold), which is confusing otherwise.
        raise HTTPException(404, f"'{path}' is not present in this run's workspace — it may "
                                 "have been written inside the container rather than to the "
                                 "mounted workspace")
    if target.stat().st_size > 2_000_000:
        raise HTTPException(413, "file too large to preview")
    try:
        text = target.read_text(errors="replace")
    except Exception:
        raise HTTPException(415, "not a text file")
    return {"path": path, "size": target.stat().st_size, "content": text}


@app.get("/api/poll")
def poll(since: int = 0):
    """Polling alternative to SSE.

    SSE is elegant but fragile: browsers, proxies and extensions can all close a long-lived
    stream, and a dropped stream leaves the UI frozen. This endpoint returns any events after
    a cursor, so the UI can simply ask again every second. It cannot be 'closed' — each call
    is an ordinary short request.
    """
    evs, cursor = BUS.since(since)
    return {"events": evs, "cursor": cursor}


@app.get("/api/events")
def events():
    q = BUS.subscribe()
    def gen():
        # 2KB of padding: some browsers buffer a streamed response until a threshold is
        # reached before handing anything to the JS layer, which makes an SSE stream look
        # dead on arrival. A comment line is ignored by the EventSource parser.
        yield ":" + (" " * 2048) + "\n\n"
        yield "retry: 2000\n\n"
        yield ": connected\n\n"
        try:
            while True:
                try:
                    ev = q.get(timeout=10)
                    yield f"data: {json.dumps(ev)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            raise
        finally:
            BUS.unsubscribe(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
