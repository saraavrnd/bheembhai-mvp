"""Unit tests — the state machine's pure routing helpers (no DB, no runtime)."""

from types import SimpleNamespace

from bheembhai.log_keys import (
    log_key,
    progress_key,
    result_key,
    session_transcript_key,
    turn_inbox_key,
    turn_outbox_key,
)
from bheembhai.providers.local_storage import LocalStorage

from engine_service.state_machine import (
    _clear_attempt_channels,
    _env_int,
    _gate_card,
    _launch_turn_contract,
    route_next,
    steps_after,
)
from engine_service.workflow import WorkflowSpec

WF_YAML = """
workflow: wf
start: a
steps:
  - id: a
    skill: a
    model: high
    "on":
      completed: b
      BLOCK: route_to
  - id: b
    skill: b
    model: low
    "on":
      completed: c
  - id: c
    skill: c
    model: low
    "on":
      completed: DONE
"""


def spec():
    return WorkflowSpec.load_yaml(WF_YAML)


def test_route_next_uses_on_map_by_default():
    assert route_next(spec(), "a", "completed", {}) == "b"


def test_route_next_honours_hint_only_under_route_to():
    wf = spec()
    # BLOCK routes via route_to — the skill's hint IS the target
    assert route_next(wf, "a", "BLOCK", {"next_hint": "c"}) == "c"
    # completed routes via the map — the hint is advisory and ignored
    assert route_next(wf, "a", "completed", {"next_hint": "c"}) == "b"


def test_route_next_no_route_returns_none():
    assert route_next(spec(), "b", "BLOCK", {}) is None


def test_steps_after_returns_later_steps_in_order():
    assert steps_after(spec(), "a") == ["b", "c"]
    assert steps_after(spec(), "b") == ["c"]
    assert steps_after(spec(), "c") == []


def test_steps_after_unknown_target_is_empty():
    assert steps_after(spec(), "nope") == []


def test_gate_card_carries_cost_fields():
    """The reviewer card persists per-visit spend so the UI can show what the
    gated visit cost — and whether that figure is confident or scraped."""
    card = _gate_card({"role": "reviewer"}, {
        "status": "BLOCK", "summary": "not green", "cost_usd": 0.5,
        "cost_reported": True, "cost_partial": False})
    assert card["cost_usd"] == 0.5
    assert card["cost_reported"] is True
    assert card["cost_partial"] is False


def test_gate_card_cost_defaults_when_outcome_has_no_cost():
    """Pre-cost outcomes (or a lost log) must not crash the card — the fields
    default to absent/False, which the UI reads as 'unknown'."""
    card = _gate_card({"role": "reviewer"}, {"status": "BLOCK"})
    assert card["cost_usd"] is None
    assert card["cost_reported"] is False
    assert card["cost_partial"] is False


# ── Launch hygiene: clearing stale attempt channels (ADR-014) ──────────

async def test_clear_attempt_channels_deletes_all_five_keys(tmp_path):
    store = LocalStorage(base_path=str(tmp_path / "store"))
    keys = (
        result_key("r1", "story-design", 1),
        progress_key("r1", "story-design", 1),
        log_key("r1", "story-design", 1, "agent"),
        log_key("r1", "story-design", 1, "diagnostics"),
        log_key("r1", "story-design", 1, "container"),
    )
    for key in keys:
        await store.put(key, b"stale visit-1 artifact")
    await _clear_attempt_channels(store, "r1", "story-design", 1)
    for key in keys:
        assert await store.get(key) is None


async def test_clear_attempt_channels_without_store_is_noop():
    await _clear_attempt_channels(None, "r1", "story-design", 1)   # must not raise


async def test_clear_attempt_channels_survives_delete_failure(tmp_path):
    """Best-effort hygiene — a failing backend logs and continues, it must
    never turn a launch into a crash."""
    class FlakyStore:
        backend_name = "flaky"

        async def delete(self, key):
            raise OSError("boom")

    await _clear_attempt_channels(FlakyStore(), "r1", "story-design", 1)  # no raise


# ── Session launch contract (ADR-016 §2-3) ─────────────────────────────

class _RecordingStore:
    """Fake store that presigns everything and records (method, key) calls, so
    the contract test asserts WHICH keys each launch presigns."""

    def __init__(self):
        self.gets: list[str] = []
        self.puts: list[str] = []

    async def presigned_get_url(self, key, *, expires_in):
        self.gets.append(key)
        return SimpleNamespace(url=f"get://{key}")

    async def presigned_put_url(self, key, *, expires_in):
        self.puts.append(key)
        return SimpleNamespace(url=f"put://{key}")


async def test_turn_contract_fresh_launch_omits_transcript_get():
    """A fresh (non-resume) incarnation gets the turn channels + a transcript
    PUT (upload on graceful exit) but NO GET — nothing to restore (ADR-016 §3)."""
    store = _RecordingStore()
    sid = "11111111-2222-3333-4444-555555555555"
    env = await _launch_turn_contract(store, "r1", "adhoc", 1, session_id=sid)
    assert env["BB_INBOX_GET_URL"] == f"get://{turn_inbox_key('r1', 'adhoc', 1)}"
    assert env["BB_OUTBOX_PUT_URL"] == f"put://{turn_outbox_key('r1', 'adhoc', 1)}"
    assert env["BB_TRANSCRIPT_PUT_URL"] == f"put://{session_transcript_key('r1', sid)}"
    assert "BB_TRANSCRIPT_GET_URL" not in env
    assert session_transcript_key("r1", sid) not in store.gets


async def test_turn_contract_resume_presigns_transcript_get():
    """A resume incarnation (the reaper's cold-start) also gets the transcript
    GET so the container can restore + --resume the session."""
    store = _RecordingStore()
    sid = "11111111-2222-3333-4444-555555555555"
    env = await _launch_turn_contract(store, "r1", "adhoc", 2,
                                      session_id=sid, resume=True)
    assert env["BB_TRANSCRIPT_GET_URL"] == \
        f"get://{session_transcript_key('r1', sid)}"
    assert session_transcript_key("r1", sid) in store.gets
    assert session_transcript_key("r1", sid) in store.puts


async def test_turn_contract_without_session_id_has_no_transcript_channels():
    """A legacy run row without a session id still gets its turn channels —
    the transcript contract is simply absent (ADR-016 §3 mint-at-init)."""
    store = _RecordingStore()
    env = await _launch_turn_contract(store, "r1", "adhoc", 1)
    assert "BB_INBOX_GET_URL" in env and "BB_OUTBOX_PUT_URL" in env
    assert "BB_TRANSCRIPT_PUT_URL" not in env
    assert "BB_TRANSCRIPT_GET_URL" not in env


# ── _env_int: guardrail knob reads from the run's resolved env vars ─────────

def _ctx(env_vars=None):
    return SimpleNamespace(env_vars=env_vars or {})


def test_env_int_uses_resolved_value():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "1"}), "BB_MAX_STEP_VISITS", 3) == 1
    assert _env_int(_ctx({"BB_MAX_ATTEMPTS": "7"}), "BB_MAX_ATTEMPTS", 2) == 7


def test_env_int_defaults_when_absent():
    assert _env_int(_ctx(), "BB_MAX_STEP_VISITS", 3) == 3


def test_env_int_clamps_to_positive():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "0"}), "BB_MAX_STEP_VISITS", 3) == 1
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "-4"}), "BB_MAX_STEP_VISITS", 3) == 1


def test_env_int_falls_back_on_garbage():
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": "lots"}), "BB_MAX_STEP_VISITS", 3) == 3
    assert _env_int(_ctx({"BB_MAX_STEP_VISITS": ""}), "BB_MAX_STEP_VISITS", 3) == 3
