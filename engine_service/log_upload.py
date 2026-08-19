"""Object-storage log registration — the engine side of the ADR-011/ADR-014 log pipeline.

Uploading moved into the step containers (ADR-014): the agent PUTs agent.log and
diagnostics.txt to their FINAL keys via presigned PUT URLs, and the engine captures
container.log from the docker API straight into storage. The engine's job here is
reference-row registration only — recording which attempt artifacts exist so the
platform's logs endpoint can serve them.

Log artifacts are auxiliary: a failed registration must never fail the step (the
push-lands-or-retry invariant concerns git, not logs). Everything is best-effort,
logged, and idempotent — crash re-entry re-reads the same keys and the run_logs
unique constraint dedupes the reference row.
"""

import logging
import traceback

from bheembhai.log_keys import KINDS, log_key
from bheembhai.models.run import RunLog
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def upload_step_logs(session, run, step_id: str, attempt_no: int,
                           store) -> int:
    """Register RunLog reference rows for this attempt's artifacts, which were
    uploaded directly to their FINAL object-store keys — agent.log and
    diagnostics.txt by the agent (presigned PUTs), container.log by the engine.
    A row is added only when the object actually exists (the platform's logs
    endpoint would 404 a stale reference otherwise), in the CURRENT transaction
    — the caller commits them with the step's transition, so a crash can never
    leave an artifact without a pointer. Returns the number of rows added or
    confirmed (an existing row is never duplicated). Never raises."""
    if store is None:
        return 0
    added = 0
    for kind in KINDS:
        key = log_key(str(run.id), step_id, attempt_no, kind)
        try:
            head = await store.head(key)
        except Exception:  # noqa: BLE001 — best-effort bookkeeping must not fail the run
            logger.warning(
                "log head failed run=%s step=%s attempt=%s kind=%s key=%s:\n%s",
                run.id, step_id, attempt_no, kind, key, traceback.format_exc())
            continue
        if head is None or head.size <= 0:
            continue  # never uploaded — no pointer for a 404-in-waiting
        try:
            existing = await session.scalar(
                select(RunLog).where(
                    RunLog.run_id == run.id,
                    RunLog.step_id == step_id,
                    RunLog.attempt_no == attempt_no,
                    RunLog.kind == kind))
            if existing is None:
                session.add(RunLog(
                    run_id=run.id, step_id=step_id, attempt_no=attempt_no,
                    kind=kind, object_key=key, size_bytes=head.size))
            added += 1
        except Exception:  # noqa: BLE001 — best-effort log bookkeeping must not fail the run
            logger.warning(
                "run_logs reference insert failed run=%s step=%s attempt=%s kind=%s:\n%s",
                run.id, step_id, attempt_no, kind, traceback.format_exc())
    return added
