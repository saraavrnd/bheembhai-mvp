"""
BheemBhai MVP engine — workflow execution over containerized skills.

  - Two independent signals: result payload (from container) + exit status (from runtime).
    A crashed container cannot report its own death, so exit status comes from Runtime.status().
  - The reconciler joins those signals against a deadline to classify each attempt.
  - Backend owns routing: the workflow table is authoritative; result 'next' hints are advisory.
  - Policy is an overlay: it never changes what runs, only where a human gates.
"""
import json, os, queue, sqlite3, threading, time, uuid, logging, traceback
from dataclasses import dataclass, field
from pathlib import Path
import yaml

logging.basicConfig(
    level=os.environ.get("BB_LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S")
log = logging.getLogger("bheembhai")

WORKDIR = Path(os.environ.get("BB_WORKDIR", "/tmp/bheembhai"))
DB_PATH = WORKDIR / "bheembhai.db"
# The orchestrator's control-plane result file. Deliberately NOT "result.json":
# the PDLC skills use result.json as their own in-repo handoff artifact, and an agent
# will happily overwrite a file by that name. Keep the control plane in its own namespace.
RESULT_FILENAME = "bb_step_result.json"
GRACE_SECONDS = 3.0
POLL_INTERVAL = 0.4


class ExecState:
    PENDING = "pending"; RUNNING = "running"
    AWAITING_RESULT = "awaiting_result"; AWAITING_APPROVAL = "awaiting_approval"
    RETRYING = "retrying"; COMPLETED = "completed"; FAILED = "failed"


class Result:
    COMPLETED = "completed"; BLOCK = "BLOCK"
    CHANGES_REQUESTED = "changes_requested"; ESCALATION_REQUIRED = "escalation_required"
    FAILED_EXECUTION = "failed_execution"      # deterministic
    FAILED_INFRA = "failed_infra"              # transient
    FAILED_TIMEOUT = "failed_timeout"          # transient
    FAILED_INCOMPLETE = "failed_incomplete"    # transient


TRANSIENT = {Result.FAILED_INFRA, Result.FAILED_TIMEOUT, Result.FAILED_INCOMPLETE}


def db():
    WORKDIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, workflow TEXT, policy TEXT, state TEXT,
  current_step TEXT, cost_usd REAL DEFAULT 0, created_at REAL, repo TEXT);
CREATE TABLE IF NOT EXISTS steps (
  id TEXT PRIMARY KEY, run_id TEXT, step_id TEXT, skill TEXT,
  exec_state TEXT, result_status TEXT, cost_usd REAL DEFAULT 0,
  attempt_no INTEGER DEFAULT 1, started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, step_id TEXT,
  attempt_no INTEGER, from_state TEXT, to_state TEXT, result_status TEXT,
  actor TEXT, reason TEXT, ts REAL);
"""


def init_db():
    conn = db(); conn.executescript(SCHEMA); conn.commit(); conn.close()


class EventBus:
    """Fan-out with a replay buffer.

    EventSource reconnects transparently, and each reconnect is a BRAND-NEW subscription.
    Without history a reconnecting browser only sees future events — so the UI, which was
    built from the events it missed, appears frozen even though the run is healthy. Keep a
    bounded backlog and replay it to every new subscriber.
    """
    REPLAY = 400

    def __init__(self):
        self._subs = []; self._lock = threading.Lock()
        self._history = []
        self._seq = 0
    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            for ev in self._history:      # catch the newcomer up
                q.put(ev)
            self._subs.append(q)
        return q
    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs: self._subs.remove(q)
    def since(self, cursor):
        """Events after `cursor`, plus the new cursor. Backs the polling endpoint."""
        with self._lock:
            base = self._seq - len(self._history)
            start = max(0, cursor - base)
            return list(self._history[start:]), self._seq

    def publish(self, event):
        with self._lock:
            self._seq += 1
            self._history.append(event)
            if len(self._history) > self.REPLAY:
                del self._history[:len(self._history) - self.REPLAY]
            subs = list(self._subs)
        for q in subs: q.put(event)


BUS = EventBus()


def record(run_id, step_id, attempt_no, from_state, to_state,
           result_status=None, actor="system", reason=None, **extra):
    conn = db()
    conn.execute(
        "INSERT INTO transitions (run_id, step_id, attempt_no, from_state, to_state,"
        " result_status, actor, reason, ts) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, step_id, attempt_no, from_state, to_state, result_status,
         actor, reason, time.time()))
    conn.commit(); conn.close()
    BUS.publish({"type": "transition", "run_id": run_id, "step_id": step_id,
                 "attempt_no": attempt_no, "from": from_state, "to": to_state,
                 "result_status": result_status, "actor": actor, "reason": reason,
                 "ts": time.time(), **extra})


@dataclass
class Handle:
    container_id: str
    result_path: Path
    started_at: float


class DockerRuntime:
    """launch() / status() only — everything above this is runtime-agnostic."""
    def __init__(self, image, endpoint=None):
        import docker
        self.client = docker.DockerClient(base_url=endpoint) if endpoint else docker.from_env()
        self.image = image
    def launch(self, run_id, step_id, attempt_no, skill, workspace, context=None, story_id=None, model=None, git=None, profile_env=None):
        outdir = WORKDIR / "results" / run_id / step_id / str(attempt_no)
        outdir.mkdir(parents=True, exist_ok=True)
        # The container runs as a NON-ROOT user (so Claude Code will accept
        # --dangerously-skip-permissions). Host-created mounts must therefore be writable
        # by that user, or the container can't publish its result.
        try:
            os.chmod(outdir, 0o777)
        except Exception:
            pass
        env = {"RUN_ID": run_id, "STEP_ID": step_id, "ATTEMPT_NO": str(attempt_no),
               "SKILL": skill, "RESULT_DIR": "/out"}
        if story_id:
            env["STORY_ID"] = story_id
        if model:
            env["BB_MODEL"] = model
        env["BB_ACTIVE_PROFILE"] = os.environ.get("BB_MODEL_PROFILE", "anthropic")
        # Git mode: the container clones the run branch itself (creating it from the source
        # branch on the first step). We give it the coordinates; it does the checkout,
        # commit, and push. The workspace is a fresh empty host dir the container clones INTO,
        # so the host can still read artifacts back from it after the run.
        if git:
            env["BB_GIT_MODE"] = "1"
            env["GIT_REMOTE_URL"] = git["url"]
            env["GIT_SOURCE_BRANCH"] = git["source_branch"]
            env["RUN_BRANCH"] = git["run_branch"]
            workspace = WORKDIR / "clones" / run_id / step_id / str(attempt_no)
            workspace.mkdir(parents=True, exist_ok=True)
            try: os.chmod(workspace, 0o777)
            except Exception: pass
            # the container clones into <workspace>/repo; artifacts live there
            WORKSPACES[run_id] = str(workspace / "repo")
        # Per-run CONTEXT (see DESIGN note): the backend tells the skill its valid
        # result-status vocabulary and whether a human gate follows — so the skill emits
        # only routable statuses and can write its summary for a reviewer. It does NOT
        # include routing targets: the skill learns what it may SAY and who's LISTENING,
        # never where its words route the run. Written to /ctx/context.json; a compact
        # copy also goes in BB_CONTEXT for convenience.
        if context:
            ctxdir = WORKDIR / "context" / run_id / step_id / str(attempt_no)
            ctxdir.mkdir(parents=True, exist_ok=True)
            try: os.chmod(ctxdir, 0o755)
            except Exception: pass
            (ctxdir / "context.json").write_text(json.dumps(context, indent=2))
            env["BB_CONTEXT"] = json.dumps(context, separators=(",", ":"))
            env["CONTEXT_FILE"] = "/ctx/context.json"
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "BB_MOCK",
                  "BB_MOCK_SECONDS", "BB_MOCK_FORCE",
                  # Vendor auth: profiles that set ANTHROPIC_BASE_URL (DeepSeek, Kimi, …)
                  # authenticate with ANTHROPIC_AUTH_TOKEN, kept in the host env / a secret,
                  # never in the committed profile file. Forward it and the CLAUDE_CODE knobs.
                  "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_SUBAGENT_MODEL",
                  "CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL",
                  # MCP credentials — injected per run, never baked into the image.
                  "JIRA_URL", "JIRA_USERNAME", "JIRA_EMAIL", "JIRA_API_TOKEN", "GH_TOKEN"):
            if os.environ.get(k): env[k] = os.environ[k]
        # Profile env applied LAST so the deployment's declared vendor endpoint/knobs win over
        # any stale host ANTHROPIC_BASE_URL. The secret token (ANTHROPIC_AUTH_TOKEN) still comes
        # from the host env above and is deliberately absent from profile files, so it is not
        # overwritten here.
        if profile_env:
            for k, v in profile_env.items():
                env[k] = v
        vols = {str(outdir): {"bind": "/out", "mode": "rw"},
                str(workspace): {"bind": "/workspace", "mode": "rw"}}
        if context:
            vols[str(ctxdir)] = {"bind": "/ctx", "mode": "ro"}
        log.info("launch step=%s attempt=%s image=%s", step_id, attempt_no, self.image)
        log.info("  result path (host): %s", outdir / RESULT_FILENAME)
        log.info("  mounts: %s", {k: v["bind"] for k, v in vols.items()})
        try:
            c = self.client.containers.run(
                self.image, detach=True, environment=env, volumes=vols,
                working_dir="/workspace",
                mem_limit=os.environ.get("BB_MEM_LIMIT", "4g"),
                network_mode=os.environ.get("BB_NETWORK", "bridge"))
        except Exception:
            log.error("launch FAILED for step=%s:\n%s", step_id, traceback.format_exc())
            raise
        log.info("  container started id=%s", c.id[:12])
        return Handle(c.id, outdir / RESULT_FILENAME, time.time())
    def status(self, h):
        import docker
        try:
            c = self.client.containers.get(h.container_id)
        except docker.errors.NotFound:
            return {"state": "gone", "exit_code": None}
        c.reload()
        if c.status == "running":
            return {"state": "running", "exit_code": None}
        return {"state": "exited", "exit_code": c.attrs.get("State", {}).get("ExitCode")}
    def logs(self, h, tail=40):
        try:
            return self.client.containers.get(h.container_id).logs(tail=tail).decode("utf-8", "replace")
        except Exception:
            return ""
    def cleanup(self, h):
        # BB_KEEP_CONTAINERS=1 leaves containers around for post-mortem inspection
        # (docker exec / docker logs). They are ephemeral by design, so this is a
        # debugging aid only — remember to `docker container prune` afterwards.
        if os.environ.get("BB_KEEP_CONTAINERS") == "1":
            log.info("  keeping container %s for inspection (BB_KEEP_CONTAINERS=1)",
                     h.container_id[:12])
            return
        try: self.client.containers.get(h.container_id).remove(force=True)
        except Exception: pass


def read_result(path):
    if not path.exists(): return None
    try: return json.loads(path.read_text())
    except Exception: return None


def reconcile(runtime, h, deadline_s, on_progress=None):
    """Poll until terminal, then classify by joining result + exit status.

    on_progress(dict) is called when the container's progress.json changes, so a
    long-running stage reports liveness instead of going silent for minutes.
    """
    exited_at = None
    polls = 0
    last_progress = None
    progress_path = h.result_path.parent / "progress.json"
    log.info("reconcile start: watching container=%s deadline=%ss result=%s",
             h.container_id[:12], deadline_s, h.result_path)
    while True:
        polls += 1
        try:
            st = runtime.status(h)
        except Exception:
            log.error("status() raised — treating as infra failure:\n%s",
                      traceback.format_exc())
            return {"status": Result.FAILED_INFRA, "reason": "runtime status() error"}
        payload = read_result(h.result_path)
        elapsed = time.time() - h.started_at

        # container heartbeat — surfaces "still working" instead of dead air
        prog = read_result(progress_path)
        if prog and prog != last_progress:
            last_progress = prog
            log.info("  progress: phase=%s %s (%ss)", prog.get("phase"),
                     prog.get("note", ""), prog.get("elapsed_s"))
            if on_progress:
                try: on_progress(prog)
                except Exception: pass

        if polls == 1 or polls % 10 == 0 or st["state"] != "running":
            log.info("  poll #%d: state=%s exit=%s result_present=%s elapsed=%.1fs",
                     polls, st["state"], st.get("exit_code"),
                     payload is not None, elapsed)

        if st["state"] == "gone":
            log.warning("container gone without result -> failed_infra")
            return {"status": Result.FAILED_INFRA,
                    "reason": "container vanished (OOM / host lost)"}

        if st["state"] == "exited":
            exited_at = exited_at or time.time()
            if payload:
                status = payload.get("status", Result.COMPLETED)
                if st["exit_code"] not in (0, None) and status == Result.COMPLETED:
                    status = Result.FAILED_EXECUTION
                log.info("  -> classified '%s' (exit=%s)", status, st.get("exit_code"))
                return {"status": status,
                        "cost_usd": float(payload.get("cost_usd") or 0),
                        "next_hint": payload.get("next"),
                        "artifact": payload.get("artifact"),
                        "summary": payload.get("summary"),
                        "files": payload.get("files") or [],
                        # What the skill wants a human to actually review — a curated subset
                        # (or superset with context files), each optionally annotated. When a
                        # skill emits this, the UI shows these by default instead of every
                        # git-touched file. Absent -> UI falls back to `files`.
                        "review_files": payload.get("review_files") or [],
                        "commit": payload.get("commit"),
                        "reason": payload.get("reason")}
            if time.time() - exited_at < GRACE_SECONDS:
                time.sleep(POLL_INTERVAL); continue
            log.warning("  exited (exit=%s) but NO %s at %s -> failed_incomplete",
                        st.get("exit_code"), RESULT_FILENAME, h.result_path)
            return {"status": Result.FAILED_INCOMPLETE,
                    "reason": f"exited ({st['exit_code']}) without publishing a result"}

        if elapsed > deadline_s:
            log.warning("  deadline exceeded (%.1fs > %ss) -> failed_timeout", elapsed, deadline_s)
            runtime.cleanup(h)
            return {"status": Result.FAILED_TIMEOUT, "reason": f"exceeded {deadline_s}s deadline"}
        time.sleep(POLL_INTERVAL)


@dataclass
class Workflow:
    name: str; start: str; steps: dict
    @staticmethod
    def load(path):
        d = yaml.safe_load(Path(path).read_text())
        steps = {}
        for s in d["steps"]:
            if True in s and "on" not in s:   # YAML 1.1 reads bare `on:` as boolean True
                s["on"] = s.pop(True)
            steps[s["id"]] = s
        return Workflow(d["workflow"], d["start"], steps)

    def allowed_statuses(self, step_id):
        """The result statuses this step may emit that the workflow knows how to route.

        This is the skill's VALID VOCABULARY for this run — the keys of its `on:` block.
        Deliberately returns the status NAMES only, never their targets: the skill learns
        what it may say, not where each choice leads. `completed` is always allowed (a step
        can always succeed); failure statuses are engine-level and not skill-selectable here.
        """
        spec = self.steps.get(step_id, {})
        keys = set((spec.get("on") or {}).keys())
        keys.add(Result.COMPLETED)
        return sorted(keys)


@dataclass
class Policy:
    name: str; gates: dict = field(default_factory=dict)
    @staticmethod
    def load(path):
        d = yaml.safe_load(Path(path).read_text())
        return Policy(d["policy"], d.get("gates") or {})

    def gate_for(self, step_id, status):
        """The gate that applies to this step for this outcome, or None.

        A gate may declare `on_status: [...]` to require a human on non-happy outcomes
        (BLOCK, changes_requested, escalation_required) as well as on success. Defaults to
        ["completed"], so existing policies behave exactly as before.

        Note the boundary: policy decides WHETHER a human is consulted; the workflow still
        decides WHERE control goes afterwards.
        """
        gate = self.gates.get(step_id)
        if not gate:
            return None
        applies = gate.get("on_status") or [Result.COMPLETED]
        return gate if status in applies else None


class PairingError(ValueError):
    """A workflow and policy that don't fit together — caught at load, not at runtime."""


class WorkflowError(ValueError):
    """A malformed or internally-inconsistent workflow — caught at load, not mid-run."""


def load_model_profile():
    """Load the active model profile — a KEY=value file selected by BB_MODEL_PROFILE.

    A profile makes the platform vendor-neutral: the workflow names a TIER (opus/sonnet/
    haiku), and the profile decides which real model each tier resolves to, plus the
    endpoint/auth env the vendor needs (ANTHROPIC_BASE_URL, CLAUDE_CODE_* …). Switching
    vendor is 'point BB_MODEL_PROFILE at a different file' — no code change, no workflow
    change. Secrets (ANTHROPIC_AUTH_TOKEN) come from the environment, NOT the profile file.

    Returns (tier_map, passthrough_env):
      tier_map        -> {"opus": <model>, "sonnet": <model>, "haiku": <model>}
      passthrough_env -> the non-BB_TIER_* lines, forwarded verbatim into the container
    """
    name = os.environ.get("BB_MODEL_PROFILE", "anthropic")
    # allow either a bare name (-> config/profiles/<name>.env) or an absolute path
    p = Path(name)
    if not p.is_absolute():
        p = Path(__file__).parent / "config" / "profiles" / f"{name}.env"
    tier_map, passthrough = {}, {}
    if p.is_file():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "BB_TIER_OPUS":   tier_map["opus"] = v
            elif k == "BB_TIER_SONNET": tier_map["sonnet"] = v
            elif k == "BB_TIER_HAIKU":  tier_map["haiku"] = v
            else:
                passthrough[k] = v      # ANTHROPIC_BASE_URL, CLAUDE_CODE_*, etc.
    else:
        log.warning("model profile '%s' not found at %s — using Anthropic defaults", name, p)
        tier_map = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6",
                    "haiku": "claude-haiku-4-5"}
    return tier_map, passthrough


# workflow model names -> tier. Lets a workflow keep saying claude-*-N (readable, the default
# profile) while a vendor profile remaps the tier to its own model.
_TIER_ALIASES = {
    "claude-opus-4-8": "opus", "opus": "opus",
    "claude-sonnet-4-6": "sonnet", "sonnet": "sonnet",
    "claude-haiku-4-5": "haiku", "haiku": "haiku",
}


def resolve_model(workflow_model, tier_map):
    """Resolve a workflow's `model:` to the actual model id for the active profile.

    If it names a tier (or a claude-* alias), map through the profile. If it's something
    else (a literal vendor model a workflow chose to hardcode), pass it through unchanged.
    """
    if not workflow_model:
        return None
    tier = _TIER_ALIASES.get(workflow_model)
    if tier and tier in tier_map:
        return tier_map[tier]
    return workflow_model    # literal / unknown -> pass through as-is


def _known_models():
    """Allowed model ids, from BB_ALLOWED_MODELS (comma-separated), else a sensible default.

    Sourced from env so new models — including non-Anthropic vendors (OpenAI, Kimi, …) —
    are added by extending a variable, not editing code. Empty/unset falls back to the
    current Anthropic set. Set BB_ALLOWED_MODELS='*' to disable the check entirely.
    """
    raw = os.environ.get("BB_ALLOWED_MODELS", "").strip()
    if raw == "*":
        return None   # sentinel: accept anything
    if raw:
        return {m.strip() for m in raw.split(",") if m.strip()}
    return {"claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"}


def check_startup_config():
    """Validate env configuration ONCE at boot, so obvious mistakes surface immediately with
    a clear message — not per-request, buried in a workflow-rejection later.

    Returns a list of human-readable problems (empty = all good). The caller decides whether
    to warn or refuse to start.
    """
    problems = []

    raw = os.environ.get("BB_ALLOWED_MODELS", "").strip()
    if raw and raw != "*":
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        # A missing comma glues two ids into one long token — the exact mistake that produced
        # 'claude-sonnet-4-6claude-haiku-4-5'. Heuristics: a token with no separators that is
        # very long, or that contains a second 'claude-'/'gpt-'/'-4' mid-string, is suspect.
        for t in tokens:
            looks_glued = (len(t) > 40) or (t.count("claude-") > 1) or (t.count("gpt-") > 1)
            if looks_glued:
                problems.append(
                    f"BB_ALLOWED_MODELS token looks like two ids joined by a missing comma: "
                    f"'{t}'. Separate models with commas, e.g. "
                    f"'claude-opus-4-8,claude-sonnet-4-6,claude-haiku-4-5'.")
        if not tokens:
            problems.append("BB_ALLOWED_MODELS is set but empty after parsing.")

    if os.environ.get("BB_GIT_MODE", "1") != "0":
        if not os.environ.get("GIT_REMOTE_URL"):
            problems.append("git mode is on (BB_GIT_MODE!=0) but GIT_REMOTE_URL is not set.")

    return problems


def validate_workflow(workflow, known_skills=None):
    """Reject a malformed workflow before any container launches.

    Catches, at load time, the classes of error that would otherwise surface mid-run after
    money has been spent: a routing target that doesn't exist, an unknown model (your $13
    lesson), a start step that isn't defined, a step missing its skill. All cheap to check,
    all far better as an instant clear error than a container launch followed by a halt.
    """
    problems = []
    steps = workflow.steps or {}

    # structure: start resolves, every step has id + skill
    if not workflow.start or workflow.start not in steps:
        problems.append(f"start step '{workflow.start}' is not defined")
    for sid, spec in steps.items():
        if not spec.get("skill"):
            problems.append(f"step '{sid}' has no skill")

    # routing: every on: target resolves to a real step (or route_to / DONE)
    for sid, spec in steps.items():
        for status, target in (spec.get("on") or {}).items():
            if target in ("route_to", "DONE"):
                continue
            if target not in steps:
                problems.append(
                    f"step '{sid}' routes '{status}' -> '{target}', which is not a defined step")

    # models: every declared model is on the allowlist (unless disabled with '*')
    allowed = _known_models()
    if allowed is not None:
        for sid, spec in steps.items():
            m = spec.get("model")
            if m and m not in allowed:
                problems.append(
                    f"step '{sid}' uses model '{m}', not in the allowed set {sorted(allowed)} "
                    f"(set BB_ALLOWED_MODELS to extend, or '*' to disable this check)")

    # skills: every skill is one the platform knows about (when a list is provided)
    if known_skills is not None:
        ks = set(known_skills)
        for sid, spec in steps.items():
            sk = spec.get("skill")
            if sk and sk not in ks:
                problems.append(f"step '{sid}' uses skill '{sk}', which is not installed")

    if problems:
        raise WorkflowError(
            "workflow is invalid:\n  - " + "\n  - ".join(problems))
    return True


def validate_pairing(workflow, policy):
    """Reject a workflow+policy pairing whose gates can't be honoured.

    The rule: policy governs WHETHER a human weighs in on a transition the workflow defines.
    It cannot invent transitions. So a gate's `on_status` may only list statuses the workflow
    can actually route from that step (its `on:` keys, plus `completed`, which always exists).

    Without this check, a policy could pause for a human on, say, story-design→BLOCK when the
    workflow has no BLOCK route there — the reviewer approves, and the engine then halts with
    'no route defined', having wasted the review. Catching it here makes that impossible to
    ship rather than latent until the status happens to fire.
    """
    problems = []
    for step_id, gate in (policy.gates or {}).items():
        if step_id not in workflow.steps:
            problems.append(
                f"policy '{policy.name}' gates step '{step_id}', which workflow "
                f"'{workflow.name}' does not define")
            continue
        routable = set(workflow.allowed_statuses(step_id))   # on: keys + completed
        for st in (gate.get("on_status") or [Result.COMPLETED]):
            if st not in routable:
                problems.append(
                    f"policy '{policy.name}' gate '{step_id}' waits for review on '{st}', "
                    f"but workflow '{workflow.name}' has no route for '{st}' from '{step_id}' "
                    f"(routable: {sorted(routable)}). A human would approve into a dead end.")
    if problems:
        raise PairingError(
            "workflow/policy pairing is inconsistent:\n  - " + "\n  - ".join(problems))
    return True


class Run:
    MAX_ATTEMPTS = 2
    def __init__(self, workflow, policy, runtime, repo, run_id=None, story_id=None, git=None):
        self.id = run_id or uuid.uuid4().hex[:12]
        self.wf = workflow; self.policy = policy; self.runtime = runtime
        self.repo = repo; self.cost = 0.0; self.state = "queued"
        self.git = git    # {url, source_branch, run_branch} in git mode, else None
        # Model profile: resolves workflow tier names to this deployment's actual models and
        # carries the vendor's endpoint/env. Loaded once per run so every step is consistent.
        self.tier_map, self.profile_env = load_model_profile()
        # In git mode there is no host workspace to read from — artifacts are read from the
        # per-step clone the runtime keeps. In copy mode, remember the workspace path.
        if repo is not None:
            WORKSPACES[self.id] = str(repo)
        self.story_id = story_id
        self.current = workflow.start
        self._approval = threading.Event(); self._approval_decision = None
        self._approval_comment = ""      # reviewer's revision notes, passed to the next attempt
        self._handoff = None             # prior step's verdict+report, passed to the step it routes to
        self._save()
    def _save(self):
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO runs (id, workflow, policy, state, current_step,"
            " cost_usd, created_at, repo) VALUES (?,?,?,?,?,?,?,?)",
            (self.id, self.wf.name, self.policy.name, self.state, self.current,
             self.cost, time.time(), str(self.repo)))
        conn.commit(); conn.close()
    def _save_step(self, step_id, skill, exec_state, result_status=None, cost=0.0, attempt=1):
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO steps (id, run_id, step_id, skill, exec_state,"
            " result_status, cost_usd, attempt_no, started_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"{self.id}:{step_id}", self.id, step_id, skill, exec_state,
             result_status, cost, attempt, time.time()))
        conn.commit(); conn.close()
    def approve(self, decision="approve", actor="reviewer", comment=""):
        self._approval_decision = decision
        self._approval_comment = comment
        self._approval.set()
    def start(self):
        threading.Thread(target=self._loop_guarded, daemon=True).start(); return self.id

    def _loop_guarded(self):
        try:
            self._loop()
        except Exception:
            log.error("run loop CRASHED for run=%s:\n%s", self.id, traceback.format_exc())
            self.state = "failed"
            try:
                self._save()
                record(self.id, self.current, None, "running", "failed",
                       reason="engine error — see server logs")
                BUS.publish({"type": "run_finished", "run_id": self.id,
                             "state": "failed", "cost_usd": round(self.cost, 4)})
            except Exception:
                log.error("failed to record crash:\n%s", traceback.format_exc())
    def _emit_plan(self):
        BUS.publish({"type": "plan", "run_id": self.id,
                     "workflow": self.wf.name, "policy": self.policy.name,
                     "steps": [{"id": sid, "skill": s.get("skill", sid),
                                "label": s.get("label", sid.replace("-", " ")),
                                "gated": sid in self.policy.gates}
                               for sid, s in self.wf.steps.items()]})
    def _loop(self):
        self.state = "running"; self._save(); self._emit_plan()
        record(self.id, None, None, "queued", "running", reason="run started")
        step_id = self.wf.start; guard = 0
        # Per-step visit counter. A workflow can legitimately loop (test-verify BLOCK ->
        # implement -> test-verify), but an agent that keeps emitting the same non-happy
        # status will cycle forever and burn budget. Cap how many times any single step
        # runs, then halt for a human. This is the seatbelt for the runaway-loop case.
        visits = {}
        max_visits = int(os.environ.get("BB_MAX_STEP_VISITS", "3"))
        while step_id and step_id != "DONE" and guard < 40:
            guard += 1
            spec = self.wf.steps.get(step_id)
            if not spec:
                record(self.id, step_id, None, "running", "failed", reason=f"unknown step '{step_id}'")
                self.state = "failed"; break
            visits[step_id] = visits.get(step_id, 0) + 1
            if visits[step_id] > max_visits:
                record(self.id, step_id, None, "running", "failed",
                       result_status="escalation_required",
                       reason=f"'{step_id}' ran {visits[step_id]-1}× without moving forward — "
                              f"stopping the loop for a human (limit {max_visits}). "
                              f"The run kept returning here; a person should decide what to fix.")
                self.state = "failed"; break
            outcome = self._run_step(step_id, spec)
            if outcome is None:
                self.state = "failed"; break
            status = outcome["status"]
            # Policy decides WHETHER a human is consulted for this outcome; the workflow
            # below still decides WHERE control goes afterwards.
            gate = self.policy.gate_for(step_id, status)
            if gate:
                decision = self._await_approval(step_id, gate, outcome)
                if decision == "request_changes":
                    target = spec.get("on", {}).get(Result.CHANGES_REQUESTED)
                    if not target:
                        record(self.id, step_id, None, ExecState.AWAITING_APPROVAL,
                               "failed", reason="changes requested with no route defined")
                        self.state = "failed"; break
                    step_id = target; continue
            routes = spec.get("on", {})
            target = routes.get(status)
            if target == "route_to":
                target = outcome.get("next_hint")
            if target is None:
                record(self.id, step_id, None, ExecState.COMPLETED, "failed",
                       result_status=status,
                       reason=f"no route defined for '{status}' — halting for a human")
                self.state = "failed"; break
            if target == "DONE":
                step_id = None; break
            # HAND-OFF: when a step routes onward on a non-happy verdict (BLOCK,
            # changes_requested, escalation_required), the next step is being asked to
            # ADDRESS that verdict. Carry the prior step's report so the next container knows
            # why it was invoked and where to read the detail — the skills hand off through
            # committed artifacts (e.g. test-verify writes verification.md), so we point at
            # the file and pass the summary as an inline backup.
            if status != Result.COMPLETED:
                self._handoff = {
                    "from_step": step_id,
                    "status": status,
                    "summary": outcome.get("summary") or outcome.get("reason") or "",
                    "report_files": [f["path"] for f in (outcome.get("files") or [])
                                     if str(f.get("path", "")).endswith(".md")],
                }
            else:
                self._handoff = None
            step_id = target
        if self.state != "failed":
            self.state = "completed"
        self.current = None; self._save()
        record(self.id, None, None, "running", self.state, reason="run finished",
               cost_usd=round(self.cost, 4))
        BUS.publish({"type": "run_finished", "run_id": self.id,
                     "state": self.state, "cost_usd": round(self.cost, 4)})
    def _run_step(self, step_id, spec):
        skill = spec.get("skill", step_id); deadline = int(spec.get("deadline", 900))
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self.current = step_id; self._save()
            self._save_step(step_id, skill, ExecState.RUNNING, attempt=attempt)
            record(self.id, step_id, attempt, ExecState.PENDING, ExecState.RUNNING,
                   reason=f"running {skill}", label=spec.get("label", step_id))
            # Per-run context for the skill: its valid status vocabulary + whether a human
            # gate follows this step. Vocabulary and audience — never routing targets.
            # What each outcome word MEANS. The orchestrator owns the vocabulary and defines
            # it structurally; the SKILL decides which one applies in its own domain. Note
            # these describe the MEANING of each verdict, never where it routes — routing
            # stays with the workflow so skills remain workflow-agnostic.
            status_meanings = {
                Result.COMPLETED:
                    "Your work finished successfully and the artifacts you produced are ready "
                    "for whatever comes next.",
                Result.BLOCK:
                    "A hard quality gate failed. The work cannot honestly proceed until "
                    "something upstream changes. This is a principled stop, not 'I had "
                    "trouble' — use it when proceeding would mean pretending a real problem "
                    "isn't there.",
                Result.CHANGES_REQUESTED:
                    "The work was reviewed and needs revision, but nothing is fundamentally "
                    "broken. Advisory findings that should be addressed.",
                Result.ESCALATION_REQUIRED:
                    "You hit something outside your authority: a conflict with the "
                    "architecture, a missing decision, or an ambiguity only a human or an "
                    "earlier stage can resolve.",
                Result.FAILED_EXECUTION:
                    "You genuinely could not run to completion — bad inputs, or an error you "
                    "cannot recover from.",
            }
            allowed = self.wf.allowed_statuses(step_id)
            context = {
                "run_id": self.id, "step_id": step_id, "skill": skill,
                "story_id": self.story_id,
                "reviewer_feedback": getattr(self, "_approval_comment", "") or "",
                # If this step was reached by another step's non-happy verdict, tell it why
                # and where the detail lives (e.g. test-verify's verification.md on a BLOCK).
                "upstream_handoff": (self._handoff
                                     if getattr(self, "_handoff", None)
                                     and self._handoff.get("from_step") != step_id else None),
                "allowed_result_statuses": allowed,
                "result_status_meanings": {k: v for k, v in status_meanings.items()
                                           if k in allowed},
                "gate_follows": step_id in self.policy.gates,
                "gate_role": self.policy.gates.get(step_id, {}).get("role"),
                "advice": ("A human will review this step's output — write `summary` for that "
                           "reviewer." if step_id in self.policy.gates else
                           "This step's output routes automatically; no human will read `summary` "
                           "before the next step."),
            }
            h = self.runtime.launch(self.id, step_id, attempt, skill, self.repo,
                                    context=context, story_id=self.story_id,
                                    model=resolve_model(spec.get("model"), self.tier_map),
                                    git=self.git, profile_env=self.profile_env)
            record(self.id, step_id, attempt, ExecState.RUNNING,
                   ExecState.AWAITING_RESULT, reason="container launched")
            def _progress(p, _sid=step_id, _lbl=spec.get("label", step_id)):
                BUS.publish({"type": "progress", "run_id": self.id, "step_id": _sid,
                             "label": _lbl, "phase": p.get("phase"),
                             "note": p.get("note"), "elapsed_s": p.get("elapsed_s")})

            outcome = reconcile(self.runtime, h, deadline, on_progress=_progress)
            cost = outcome.get("cost_usd", 0) or 0; self.cost += cost

            # Transparency checks — the backend stays authoritative, but never silently
            # swallows what the skill tried to say.
            st = outcome.get("status")
            allowed = set(self.wf.allowed_statuses(step_id))
            engine_statuses = TRANSIENT | {Result.FAILED_EXECUTION}
            if st not in allowed and st not in engine_statuses:
                # skill emitted a status this workflow can't route AND it isn't an engine
                # failure — flag it (it will hit the no-route halt below, now explained).
                record(self.id, step_id, attempt, ExecState.AWAITING_RESULT,
                       ExecState.AWAITING_RESULT, result_status=st,
                       reason=f"skill emitted '{st}', outside this step's allowed set "
                              f"{sorted(allowed)} — the skill was given this vocabulary but "
                              f"stepped outside it")
            hint = outcome.get("next_hint")
            routes_here = spec.get("on", {})
            if hint and routes_here.get(st) != "route_to":
                # the skill suggested a next step but the workflow doesn't delegate here —
                # backend wins, but the suggestion is preserved, not dropped.
                record(self.id, step_id, attempt, ExecState.AWAITING_RESULT,
                       ExecState.AWAITING_RESULT, result_status=st,
                       reason=f"skill suggested next='{hint}'; workflow is authoritative and "
                              f"routes '{st}' its own way — suggestion noted, not taken")
            logs = ""
            if outcome["status"] not in (Result.COMPLETED, Result.BLOCK,
                                         Result.CHANGES_REQUESTED, Result.ESCALATION_REQUIRED):
                logs = self.runtime.logs(h)
            self.runtime.cleanup(h)
            status = outcome["status"]
            self._save_step(step_id, skill, ExecState.COMPLETED, status, cost, attempt)
            self._save()
            record(self.id, step_id, attempt, ExecState.AWAITING_RESULT,
                   ExecState.COMPLETED if status == Result.COMPLETED else ExecState.FAILED,
                   result_status=status, reason=outcome.get("reason"),
                   cost_usd=round(cost, 4), run_cost=round(self.cost, 4),
                   summary=outcome.get("summary"), artifact=outcome.get("artifact"),
                   files=outcome.get("files") or [], commit=outcome.get("commit"),
                   review_files=outcome.get("review_files") or [],
                   logs=logs[-1200:] if logs else None)
            if status in TRANSIENT and attempt < self.MAX_ATTEMPTS:
                record(self.id, step_id, attempt, ExecState.FAILED, ExecState.RETRYING,
                       result_status=status,
                       reason="transient failure — retrying in a fresh container")
                continue
            if status in TRANSIENT or status == Result.FAILED_EXECUTION:
                record(self.id, step_id, attempt, ExecState.FAILED, ExecState.FAILED,
                       result_status=status, reason="needs a human — not retrying automatically")
                return None
            return outcome
        return None
    def _await_approval(self, step_id, gate, outcome):
        self._approval.clear(); self._approval_decision = None
        self.state = "awaiting_approval"; self._save()
        record(self.id, step_id, None, ExecState.COMPLETED, ExecState.AWAITING_APPROVAL,
               reason="waiting for your approval", role=gate.get("role", "any"),
               summary=outcome.get("summary"), artifact=outcome.get("artifact"))
        BUS.publish({"type": "approval_required", "run_id": self.id, "step_id": step_id,
                     "role": gate.get("role", "any"), "summary": outcome.get("summary"),
                     "artifact": outcome.get("artifact"),
                     "result_status": outcome.get("status"),
                     "reason": outcome.get("reason"),
                     "files": outcome.get("files") or [],
                     "review_files": outcome.get("review_files") or []})
        self._approval.wait()
        decision = self._approval_decision or "approve"
        self.state = "running"; self._save()
        cmt = getattr(self, "_approval_comment", "") or ""
        record(self.id, step_id, None, ExecState.AWAITING_APPROVAL,
               ExecState.COMPLETED if decision == "approve" else ExecState.RUNNING,
               actor="reviewer",
               reason=f"reviewer chose: {decision}" + (f" — {cmt[:400]}" if cmt else ""),
               comment=cmt)
        BUS.publish({"type": "approval_resolved", "run_id": self.id,
                     "step_id": step_id, "decision": decision, "comment": cmt,
                     "result_status": outcome.get("status")})
        return decision


RUNS = {}
WORKSPACES = {}   # run_id -> workspace path, for reading artifacts back
