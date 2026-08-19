"""Shared test fixtures — available to all test layers."""

import json
import time

import pytest
from bheembhai.log_keys import result_key

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

    Launch records go to .calls (step_id, attempt_no) and .contexts (step_id, context).
    """

    def __init__(self, script=None, store=None, reattach_script=None):
        self.script = script or {}
        # Re-attach behaviours (crash resume): step_id -> behaviour stamped on the
        # rebuilt Handle. Default "gone" — a crashed container rarely survives.
        self.reattach_script = reattach_script or {}
        self.calls: list[tuple[str, int]] = []
        self.contexts: list[tuple[str, dict | None]] = []
        self.envs: list[tuple[str, dict]] = []
        self.cleaned: list[Handle] = []
        self.stopped: list[Handle] = []
        self.rehandles: list[Handle] = []
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
        return h

    async def status(self, h):
        b = getattr(h, "behaviour", "ok")
        if b == "error":
            raise RuntimeError("runtime status() boom")
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
