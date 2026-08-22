"""Shared test fixtures — available to all test layers."""

import asyncio
import json
import time

import pytest
from bheembhai.log_keys import result_key, turn_inbox_key, turn_outbox_key

from engine_service.runtime import Handle


def pytest_collection_modifyitems(config, items):
    """Mark tests by layer so `-m unit` / `-m integration` select them."""
    for item in items:
        rel = str(item.fspath)
        if "/tests/unit/" in rel:
            item.add_marker(pytest.mark.unit)
        elif "/tests/integration/" in rel:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
def app_config():
    """Return a test AppConfig with dev defaults."""
    from bheembhai.config import AppConfig
    return AppConfig.from_env()


class FakeRuntime:
    """Scriptable async Runtime — mirrors the R&D FakeRuntime (test_engine.py).

    The script maps step_id -> a list of behaviours, one per attempt:
      "ok"           -> exit 0 + completed payload
      "block"        -> exit 0 + BLOCK payload (next hint: "implement")
      "changes"      -> exit 0 + changes_requested payload
      "rogue"        -> exit 0 + payload with a status outside the step vocabulary
      "hint"         -> exit 0 + completed payload with an (ignored) next hint
      "exit-nonzero" -> exit 1 + completed payload (skill "finished" but crashed)
      "silent"       -> exit 0, no payload
      "crash"        -> exit 137, no payload
      "hung"         -> stays running forever (deadline -> failed_timeout)
      "vanish"       -> container gone
      "error"        -> status() raises (runtime failure)
      "session"      -> live multi-turn session container (ADR-016 §2): a
                        background actor answers each inbox turn with an outbox
                        reply and exits cleanly on the `end` sentinel

    Launch records go to .calls (step_id, attempt_no) and .contexts (step_id, context).
    """

    def __init__(self, script=None, store=None, reattach_script=None):
        self.script = script or {}
        # Re-attach behaviours (crash resume): step_id -> behaviour stamped on the
        # rebuilt Handle. Default "gone" — a crashed container rarely survives.
        # Session tests pass reattach_script={"<step>": "session"} so a turn that
        # follows another turn adopts the SAME live container.
        self.reattach_script = reattach_script or {}
        self.calls: list[tuple[str, int]] = []
        self.contexts: list[tuple[str, dict | None]] = []
        self.envs: list[tuple[str, dict]] = []
        self.cleaned: list[Handle] = []
        self.stopped: list[Handle] = []
        self.rehandles: list[Handle] = []
        # Background inbox actors spawned by "session" launches.
        self.session_tasks: list[asyncio.Task] = []
        # Liveness is keyed by CONTAINER ID, not per-Handle behaviour: the engine
        # re-attaches across dispatches via make_handle(), which returns a NEW
        # Handle object for the same container — docker semantics, where every
        # status() poll hits the same underlying container. The actor flips this
        # set (not its own launch handle), so re-attached handles see the exit.
        self._session_live: set[str] = set()
        # Object store the fake "containers" publish results into (ADR-014): when
        # set, launch() writes payloads at the exact agent keys so reconcile's
        # store reads exercise the real channel. None = no result publication.
        self.store = store

    def _behaviour(self, step_id: str, attempt_no: int) -> str:
        behaviours = self.script.get(step_id, ["ok"])
        return behaviours[min(attempt_no - 1, len(behaviours) - 1)]

    async def launch(self, run_id, step_id, attempt_no, env, *, context=None):
        self.calls.append((step_id, attempt_no))
        self.contexts.append((step_id, context))
        self.envs.append((step_id, dict(env)))
        b = self._behaviour(step_id, attempt_no)
        payloads = {
            "ok": {"status": "completed", "cost_usd": 0.01, "summary": f"{step_id} done"},
            "block": {"status": "BLOCK", "cost_usd": 0.01, "next": "implement",
                      "reason": "not green", "review_files": ["docs/verification.md"]},
            "changes": {"status": "changes_requested", "cost_usd": 0.01,
                        "summary": "needs revision"},
            "rogue": {"status": "out_of_vocabulary", "cost_usd": 0.01},
            "hint": {"status": "completed", "cost_usd": 0.01, "next": "somewhere-else"},
            "exit-nonzero": {"status": "completed", "cost_usd": 0.01},
        }
        # Publish at the exact agent key (ADR-014) — the "container" PUTs its
        # result to Object Storage, like the real run_skill.sh EXIT trap does.
        if b in payloads and self.store is not None:
            await self.store.put(
                result_key(str(run_id), step_id, attempt_no),
                json.dumps(payloads[b]).encode(),
                content_type="application/json")
        # "silent", "crash", "hung", "vanish", "error" write nothing
        h = Handle(f"fake-{step_id}-{attempt_no}", time.time(),
                   str(run_id), step_id, attempt_no)
        h.behaviour = b  # type: ignore[attr-defined] — dataclass without slots
        if b == "session" and self.store is not None:
            self._session_live.add(h.container_id)
            self.session_tasks.append(asyncio.create_task(
                self._session_actor(str(run_id), step_id, attempt_no, h)))
        return h

    async def _session_actor(self, run_id: str, step_id: str, attempt_no: int,
                             h: Handle) -> None:
        """The scripted agent process inside a session container (ADR-016 §2):
        polls the turn inbox at the real object-store key and answers every NEW
        seq with an outbox reply (echo + commit + per-turn cost). An `end`
        sentinel exits the process cleanly (exit 0) so the engine's exit-wait
        (_wait_container_exit) completes — liveness lives in _session_live by
        container id, so the RE-ATTACHED handle the engine waits on sees the
        exit too. Wound down by anything that discards the container id
        (end sentinel / stop())."""
        if self.store is None:
            return
        inbox_key = turn_inbox_key(run_id, step_id, attempt_no)
        outbox_key = turn_outbox_key(run_id, step_id, attempt_no)
        last = 0
        while h.container_id in self._session_live:
            raw = await self.store.get(inbox_key)
            if raw is not None:
                payload = None
                try:
                    payload = json.loads(raw.data)
                except (ValueError, UnicodeDecodeError):
                    pass
                if payload and int(payload.get("seq") or 0) > last:
                    if payload.get("kind") == "end":
                        self._session_live.discard(h.container_id)
                        return
                    if payload.get("kind") == "turn":
                        last = int(payload["seq"])
                        reply = {
                            "seq": last,
                            "response": f"echo: {payload.get('query', '')}",
                            "commit": f"abc1234{last}",
                            "files": [{"status": "M",
                                       "path": f"file-{last}.txt"}],
                            "cost_usd": 0.1,
                            "cost_reported": True,
                        }
                        await self.store.put(
                            outbox_key, json.dumps(reply).encode(),
                            content_type="application/json")
            await asyncio.sleep(0.05)

    async def status(self, h):
        b = getattr(h, "behaviour", "ok")
        if b == "error":
            raise RuntimeError("runtime status() boom")
        if b == "session":
            if h.container_id in self._session_live:
                return {"state": "running", "exit_code": None}
            # The actor exited (end sentinel) or the container was stopped —
            # a clean exit 0, the same as the real agent's graceful shutdown.
            return {"state": "exited", "exit_code": 0}
        if b == "hung":
            return {"state": "running", "exit_code": None}
        if b in ("vanish", "gone"):    # "gone" is the re-attach behaviour (crash resume)
            return {"state": "gone", "exit_code": None}
        if b == "crash":
            return {"state": "exited", "exit_code": 137}
        if b == "exit-nonzero":
            return {"state": "exited", "exit_code": 1}
        return {"state": "exited", "exit_code": 0}

    async def make_handle(self, run_id, step_id, attempt_no, container_id, started_at):
        h = Handle(container_id, started_at, str(run_id), step_id, attempt_no)
        h.behaviour = self.reattach_script.get(step_id, "gone")  # type: ignore[attr-defined]
        self.rehandles.append(h)
        return h

    async def logs(self, h, tail=40):
        return "fake logs"

    async def cleanup(self, h):
        self.cleaned.append(h)

    async def stop(self, h):
        # Cancel path — recorded separately from cleanup() (which honours
        # keep_containers; stop() must not).
        self.stopped.append(h)
        # A stopped session container is gone — discard its id so its inbox
        # actor winds down instead of answering further turns.
        self._session_live.discard(h.container_id)
        if getattr(h, "behaviour", None) == "session":
            h.behaviour = "gone"  # type: ignore[attr-defined]

    async def aclose(self):
        """Cancel any session actors still running (test hygiene)."""
        self._session_live.clear()
        for task in self.session_tasks:
            task.cancel()
        for task in self.session_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.session_tasks.clear()
