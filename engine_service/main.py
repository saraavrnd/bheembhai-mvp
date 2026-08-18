"""Engine Service — internal state machine & Fargate lifecycle (ADR-003)."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

# Root handler + level, before uvicorn takes over — same format as platform_api
# so the worker loop's claim/process lines are actually visible in the logs.
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s")

logger = logging.getLogger(__name__)

from bheembhai.config import AppConfig, load_config
from bheembhai.database import (
    close_database,
    init_database,
    run_migrations,
    seed_default_roles,
    seed_default_skills,
    seed_default_workflows,
)
from bheembhai.providers import build_object_store
from bheembhai.providers.aws_secrets import AWSSecretsManager
from bheembhai.providers.aws_ssm import AWSSSMParameterStore
from bheembhai.providers.env_secrets import EnvSecureStorage

from engine_service import notifier
from engine_service.recovery import recover_on_startup
from engine_service.routers import health
from engine_service.runtime import DockerRuntime
from engine_service.worker import configure_worker, worker_loop


def _build_secure_storage(config: AppConfig):
    """SecureStorage backend per config (ADR-012) — same selection as the platform."""
    secure_cfg = config.secure_storage
    if secure_cfg.backend == "aws_ssm":
        logger.info("SecureStorage wired: aws_ssm region=%s", secure_cfg.aws_region)
        return AWSSSMParameterStore(region=secure_cfg.aws_region)
    if secure_cfg.backend == "aws_secrets_manager":
        logger.info("SecureStorage wired: aws_secrets_manager region=%s", secure_cfg.aws_region)
        return AWSSecretsManager(region=secure_cfg.aws_region)
    return EnvSecureStorage(encrypted_config_path=secure_cfg.env_encrypted_config_path)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: load config, init DB, recover orphans, start worker loop."""
    config = load_config()
    app.state.config = config
    init_database(config.database)
    await run_migrations()
    await seed_default_roles()

    # Optional self-seeding (dev): the platform seeds its own DB, but the engine
    # can run standalone. Idempotent — seed_* are upserts.
    if config.engine.seed_on_startup:
        await seed_default_skills()
        await seed_default_workflows()

    # Runtime + SecureStorage + notifier, wired into the worker BEFORE the loop
    # starts so every dispatch task can launch containers, resolve credentials,
    # and push events.
    ec = config.engine
    runtime = DockerRuntime(
        ec.agent_image,
        endpoint=ec.docker_endpoint or None,
        workdir=ec.workdir,
        mem_limit=ec.mem_limit,
        network=ec.network,
        keep_containers=ec.keep_containers,
        env_forward=ec.env_forward,
    )
    notify_task = notifier.setup_notifier(config)
    # Object storage (ADR-011): the engine uploads each attempt's logs at
    # reconcile; the platform reads them back. AWS creds (S3 backend) come
    # from boto3's default chain — env vars or the EC2 instance role — never
    # from app config, and never into agent containers.
    store = build_object_store(config.storage)
    configure_worker(
        runtime=runtime,
        secure_storage=_build_secure_storage(config),
        publish=notifier.publish,
        store=store,
    )
    logger.info("Runtime wired: %s image=%s workdir=%s", ec.runtime, ec.agent_image, ec.workdir)
    logger.info("Object storage wired: backend=%s", getattr(store, "backend_name", "?"))

    # Crash recovery: re-enqueue stale work before starting the worker loop
    await recover_on_startup(config)

    # Start background worker loop (runs for the lifetime of the service)
    worker_task = asyncio.create_task(worker_loop(config))

    yield

    # Shutdown
    for task in (worker_task, notify_task):
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await close_database()


app = FastAPI(
    title="BheemBhai Engine Service",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
