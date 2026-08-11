"""Database engine and session factory — async SQLAlchemy 2.0 style."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bheembhai.config import DatabaseConfig
from bheembhai.models.base import Base

_logger = logging.getLogger(__name__)

_engine = None
_sessionmaker = None


def init_database(config: DatabaseConfig) -> None:
    """Initialise the async engine and session factory. Call once at startup."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.url, echo=config.echo)
    _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def run_migrations() -> None:
    """Run pending Alembic migrations against the configured database.

    Replaces ``create_tables()`` as the startup schema-management step.
    Safe to call from multiple services simultaneously — concurrent runs
    are detected and silently ignored (the second caller sees the migration
    was already applied by its peer).
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import ProgrammingError

    if _engine is None:
        raise RuntimeError("Database not initialised — call init_database first")

    # Resolve paths relative to *this file* (shared/bheembhai/database.py):
    #   shared_dir  = shared/
    #   alembic_ini = shared/alembic.ini
    #   scripts_dir = shared/alembic/
    shared_dir = Path(__file__).resolve().parent.parent
    alembic_ini = shared_dir / "alembic.ini"
    scripts_dir = shared_dir / "alembic"

    alembic_cfg = Config(str(alembic_ini))
    alembic_cfg.set_main_option("script_location", str(scripts_dir))
    alembic_cfg.set_main_option(
        "sqlalchemy.url",
        _engine.url.render_as_string(hide_password=False),
    )

    def _upgrade() -> None:
        try:
            command.upgrade(alembic_cfg, "head")
        except ProgrammingError:
            # A peer service (docker compose starts both at once) beat us to
            # the migration — the tables already exist.  Alembic recorded the
            # revision in that other transaction; our re-attempt would be a
            # no-op.  Let the caller continue.
            _logger.info(
                "Migrations already applied by a peer — continuing"
            )

    _logger.info("Running database migrations …")
    await asyncio.to_thread(_upgrade)
    _logger.info("Database migrations complete")


async def create_tables() -> None:
    """Create all ORM tables via ``Base.metadata.create_all`` (dev convenience).

    Prefer ``run_migrations()`` for production; this is kept for quick
    throwaway environments and test suites that don't need Alembic.
    """
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_database first")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def seed_default_roles() -> None:
    """Insert the default project roles if they don't already exist.

    Called at startup so FK constraints on memberships.role are always satisfied.
    """
    from sqlalchemy import select

    from bheembhai.models.user import ProjectRole

    defaults = [
        ProjectRole(key="admin", label="Admin", is_system_default=True),
        ProjectRole(key="member", label="Member", is_system_default=True),
        ProjectRole(key="viewer", label="Viewer", is_system_default=True),
    ]

    async with _sessionmaker() as session:
        for role in defaults:
            existing = await session.get(ProjectRole, role.key)
            if existing is None:
                session.add(role)
        await session.commit()


async def get_session() -> AsyncSession:  # type: ignore[empty-body]
    """Yield an async session. Used as a FastAPI dependency."""
    if _sessionmaker is None:
        raise RuntimeError("Database not initialised — call init_database first")
    async with _sessionmaker() as session:
        yield session


async def close_database() -> None:
    """Dispose the engine. Call at shutdown."""
    global _engine, _sessionmaker
    if _engine:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
