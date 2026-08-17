"""Persistence helpers shared by run init and the state machine."""

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from bheembhai.models.run import Transition

# Transition.step_id and attempt_no are NOT NULL — run-level rows use sentinels.
RUN_LEVEL_STEP = ""
RUN_LEVEL_ATTEMPT = 0


def record_transition(
    session: AsyncSession,
    run_id,
    from_state: str,
    to_state: str,
    *,
    step_id: str = RUN_LEVEL_STEP,
    attempt_no: int = RUN_LEVEL_ATTEMPT,
    result_status: str | None = None,
    actor: str = "system",
    reason: str | None = None,
    payload: dict | None = None,
) -> Transition:
    """Append an audit row. Added to the session; the caller owns the commit.

    `payload` carries structured detail that must survive restarts (ADR-003):
    step outcomes on completion rows, gate cards on awaiting_approval rows.
    """
    t = Transition(
        run_id=run_id,
        step_id=step_id,
        attempt_no=attempt_no,
        from_state=from_state,
        to_state=to_state,
        result_status=result_status,
        actor=actor,
        reason=reason,
        payload=payload,
        ts=datetime.now(timezone.utc).timestamp(),
    )
    session.add(t)
    return t
