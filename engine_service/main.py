"""Engine Service — internal state machine & Fargate lifecycle (ADR-003)."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI

from bheembhai.config import AppConfig, load_config
from bheembhai.database import close_database, create_tables, init_database, seed_default_roles

from engine_service.routers import health, webhooks
from engine_service.worker import worker_loop
from engine_service.recovery import recover_on_startup


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: load config, init DB, recover orphans, start worker loop."""
    config = load_config()
    app.state.config = config
    init_database(config.database)
    await create_tables()
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
