"""Engine Service — internal state machine & Fargate lifecycle (ADR-003)."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

# Root handler + level, before uvicorn takes over — same format as platform_api
# so the worker loop's claim/process lines are actually visible in the logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

from bheembhai.config import AppConfig, load_config
from bheembhai.database import close_database, init_database, run_migrations, seed_default_roles

from engine_service.routers import health, webhooks
from engine_service.worker import worker_loop
from engine_service.recovery import recover_on_startup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: load config, init DB, recover orphans, start worker loop."""
    config = load_config()
    app.state.config = config
    init_database(config.database)
    await run_migrations()
    await seed_default_roles()

    # Crash recovery: re-enqueue stale work before starting the worker loop
    await recover_on_startup(config)

    # Start background worker loop (runs for the lifetime of the service)
    worker_task = asyncio.create_task(worker_loop(config))

    yield

    # Shutdown
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await close_database()


app = FastAPI(
    title="BheemBhai Engine Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(webhooks.router)
