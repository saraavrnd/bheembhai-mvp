"""Object-storage log upload — the engine side of the ADR-011 log pipeline.

Log artifacts are auxiliary: a failed upload must never fail the step (the
push-lands-or-retry invariant concerns git, not logs). Every upload is
best-effort, logged, and idempotent — crash re-entry re-uploads to the same
key and the run_logs unique constraint dedupes the reference row.
"""

import asyncio
import logging
import traceback

from sqlalchemy import select

from bheembhai.log_keys import KIND_FILES, log_key
from bheembhai.models.run import RunLog

logger = logging.getLogger(__name__)


async def _put_file_offloop(store, key: str, path: str) -> None:
    """The S3 backend's boto3 call is synchronous — run it off the event loop
    (same precedent as the docker-py wrappers in engine_service/runtime.py)."""
    def _run() -> None:
        asyncio.run(store.put_file(key, path, content_type="text/plain"))
    await asyncio.to_thread(_run)


async def upload_step_logs(session, run, step_id: str, attempt_no: int,
                           handle, store) -> int:
    """Upload the attempt dir's log files (agent.log / container.log /
    diagnostics.txt) to object storage and add their reference rows to the
    CURRENT transaction — the caller commits them with the step's transition,
    so a crash can never leave an artifact without a pointer. Returns the
    number of reference rows added or confirmed (an existing row is never
    duplicated). Never raises."""
    if store is None:
        return 0
    attempt_dir = handle.result_path.parent
    added = 0
    for kind, filename in KIND_FILES.items():
        path = attempt_dir / filename
        try:
            if not path.is_file() or path.stat().st_size == 0:
                continue
        except OSError:
            continue
        key = log_key(str(run.id), step_id, attempt_no, kind)
        try:
            await _put_file_offloop(store, key, str(path))
        except Exception:
            logger.warning(
                "log upload failed run=%s step=%s attempt=%s kind=%s key=%s:\n%s",
                run.id, step_id, attempt_no, kind, key, traceback.format_exc())
            continue
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
                    kind=kind, object_key=key, size_bytes=path.stat().st_size))
            added += 1
        except Exception:
            logger.warning(
                "run_logs reference insert failed run=%s step=%s attempt=%s kind=%s:\n%s",
                run.id, step_id, attempt_no, kind, traceback.format_exc())
    return added
