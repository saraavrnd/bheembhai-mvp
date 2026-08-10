"""Verify engine logic with a fake runtime — no Docker needed."""
import os, tempfile, json, time, threading
os.environ["BB_WORKDIR"] = tempfile.mkdtemp(prefix="bbtest-")
import engine
from engine import Policy, Result, Run, Workflow
engine.init_db()


class FakeHandle:
    def __init__(self, path, started, b):
        self.result_path = path; self.started_at = started; self.container_id = "fake"; self._b = b


class FakeRuntime:
    def __init__(self, script):
        self.script = script; self.calls = []; self.contexts = []
    def launch(self, run_id, step_id, attempt_no, skill, workspace, context=None):
        self.calls.append((step_id, attempt_no))
        self.contexts.append((step_id, context))
        outdir = os.path.join(engine.WORKDIR, "r", run_id, step_id, str(attempt_no))
        os.makedirs(outdir, exist_ok=True)
        from pathlib import Path
        path = Path(outdir) / "result.json"
        behaviours = self.script.get(step_id, ["ok"])
        b = behaviours[min(attempt_no - 1, len(behaviours) - 1)]
        if b == "ok":
            path.write_text(json.dumps({"status": Result.COMPLETED, "cost_usd": 0.01, "summary": f"{skill} done"}))
        elif b == "block":
            path.write_text(json.dumps({"status": Result.BLOCK, "cost_usd": 0.01, "next": "implement", "reason": "not green"}))
        return FakeHandle(path, time.time(), b)
    def status(self, h):
        if h._b == "crash": return {"state": "exited", "exit_code": 137}
        return {"state": "exited", "exit_code": 0}
    def logs(self, h, tail=40): return "fake logs"
    def cleanup(self, h): pass


WF = Workflow.load("config/workflow-story-delivery.yaml")
STRICT = Policy.load("config/policy-strict.yaml")
FAST = Policy.load("config/policy-fast.yaml")


def run_and_wait(run, timeout=30, auto=None):
    q = engine.BUS.subscribe(); time.sleep(0.05); run.start()
    fin = {}; deadline = time.time() + timeout
    while time.time() < deadline:
        try: ev = q.get(timeout=1)
        except Exception: continue
        if ev.get("type") == "approval_required" and auto is not None:
            run.approve(auto.get(ev["step_id"], "approve"))
        if ev.get("type") == "run_finished": fin = ev; break
    engine.BUS.unsubscribe(q); return fin


def test(name, fn):
    try: fn(); print(f"  PASS  {name}"); return True
    except AssertionError as e: print(f"  FAIL  {name}: {e}"); return False


results = []

def t1():
    rt = FakeRuntime({}); r = Run(WF, STRICT, rt, "/tmp")
    fin = run_and_wait(r, auto={})
    assert fin.get("state") == "completed", fin
    steps = [c[0] for c in rt.calls]
    assert steps == ["story-design","test-creator","implement","test-verify","code-review","pr-create"], steps
    assert fin["cost_usd"] > 0
results.append(test("happy path through gates -> completed", t1))

def t2():
    rt = FakeRuntime({}); r = Run(WF, STRICT, rt, "/tmp")
    state = {"n": 0}; q = engine.BUS.subscribe(); time.sleep(0.05); r.start()
    fin = {}; deadline = time.time() + 30
    while time.time() < deadline:
        try: ev = q.get(timeout=1)
        except Exception: continue
        if ev.get("type") == "approval_required":
            if ev["step_id"] == "story-design" and state["n"] == 0:
                state["n"] = 1; r.approve("request_changes")
            else: r.approve("approve")
        if ev.get("type") == "run_finished": fin = ev; break
    engine.BUS.unsubscribe(q)
    assert fin.get("state") == "completed", fin
    assert len([c for c in rt.calls if c[0] == "story-design"]) >= 2, rt.calls
results.append(test("request_changes routes back per workflow", t2))

def t3():
    rt = FakeRuntime({"implement": ["silent", "ok"]}); r = Run(WF, FAST, rt, "/tmp")
    fin = run_and_wait(r)
    assert fin.get("state") == "completed", fin
    assert len([c for c in rt.calls if c[0] == "implement"]) == 2, rt.calls
results.append(test("silent container -> failed_incomplete -> retry succeeds", t3))

def t4():
    rt = FakeRuntime({"implement": ["crash", "crash"]}); r = Run(WF, FAST, rt, "/tmp")
    fin = run_and_wait(r)
    assert fin.get("state") == "failed", fin
    assert len([c for c in rt.calls if c[0] == "implement"]) == 2, rt.calls
results.append(test("repeated crash -> retried once then escalates", t4))

def t5():
    rt = FakeRuntime({"test-verify": ["block", "ok"]}); r = Run(WF, FAST, rt, "/tmp")
    fin = run_and_wait(r)
    assert fin.get("state") == "completed", fin
    order = [c[0] for c in rt.calls]; i = order.index("test-verify")
    assert order[i + 1] == "implement", order
results.append(test("BLOCK routes via route_to hint", t5))

def t6():
    # context is injected per step: valid vocabulary + gate-follows, no routing targets
    rt = FakeRuntime({}); r = Run(WF, STRICT, rt, "/tmp")
    run_and_wait(r, auto={})
    ctx = dict(rt.contexts)  # step_id -> context (last wins; fine, no retries here)
    sd = ctx["story-design"]
    assert "completed" in sd["allowed_result_statuses"], sd
    assert "escalation_required" in sd["allowed_result_statuses"], sd
    assert sd["gate_follows"] is True, sd                  # story-design is gated in strict
    # crucially: no routing targets leaked into the skill's context
    assert "test-creator" not in json.dumps(sd), "routing target leaked into context!"
    tc = ctx["test-creator"]
    assert tc["gate_follows"] is False, tc                 # test-creator is not gated
results.append(test("context injects vocabulary + gate flag, not routing targets", t6))

def t7():
    # a skill emitting a status the workflow can't route -> flagged, then halts (not silent)
    import io, sys as _sys
    rt = FakeRuntime({}); r = Run(WF, FAST, rt, "/tmp")
    # make test-creator emit an unroutable status
    class Rogue(FakeRuntime):
        def launch(self, run_id, step_id, attempt_no, skill, workspace, context=None):
            h = super().launch(run_id, step_id, attempt_no, skill, workspace, context)
            if step_id == "test-creator":
                from pathlib import Path
                p = Path(engine.WORKDIR)/"r"/run_id/step_id/str(attempt_no)/"result.json"
                p.write_text(json.dumps({"status": "changes_requested", "cost_usd": 0.01}))
                h._b = "ok"
            return h
    rt = Rogue({}); r = Run(WF, FAST, rt, "/tmp")
    fin = run_and_wait(r)
    # test-creator has no changes_requested route -> run halts (fail-closed), not silent
    assert fin.get("state") == "failed", fin
    # and a transition explaining the out-of-vocabulary emission was recorded
    conn = engine.db()
    rows = conn.execute("SELECT reason FROM transitions WHERE run_id=? AND reason LIKE '%outside this step%'", (r.id,)).fetchall()
    conn.close()
    assert rows, "expected a flag for the out-of-vocabulary status"
results.append(test("out-of-vocabulary status is flagged, not swallowed", t7))

def t8():
    # a skill suggesting a next step the workflow doesn't delegate -> suggestion recorded
    class Suggester(FakeRuntime):
        def launch(self, run_id, step_id, attempt_no, skill, workspace, context=None):
            h = super().launch(run_id, step_id, attempt_no, skill, workspace, context)
            if step_id == "implement":
                from pathlib import Path
                p = Path(engine.WORKDIR)/"r"/run_id/step_id/str(attempt_no)/"result.json"
                p.write_text(json.dumps({"status": "completed", "cost_usd": 0.01,
                                         "next": "security-scan"}))
                h._b = "ok"
            return h
    rt = Suggester({}); r = Run(WF, FAST, rt, "/tmp")
    fin = run_and_wait(r)
    assert fin.get("state") == "completed", fin   # backend ignores the hint, runs on
    conn = engine.db()
    rows = conn.execute("SELECT reason FROM transitions WHERE run_id=? AND reason LIKE '%suggestion noted%'", (r.id,)).fetchall()
    conn.close()
    assert rows, "expected the ignored suggestion to be recorded"
results.append(test("ignored next-hint is recorded, not dropped", t8))

print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
