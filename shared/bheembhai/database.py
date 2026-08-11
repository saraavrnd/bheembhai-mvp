"""Database engine and session factory — async SQLAlchemy 2.0 style."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bheembhai.config import DatabaseConfig
from bheembhai.models.base import Base

_engine = None
_sessionmaker = None


def init_database(config: DatabaseConfig) -> None:
    """Initialise the async engine and session factory. Call once at startup."""
    global _engine, _sessionmaker
    _engine = create_async_engine(config.url, echo=config.echo)
    _sessionmaker = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def create_tables() -> None:
    """Create all ORM tables if they don't exist (dev convenience).

    In production, migrations are managed by Alembic — this is a dev helper.
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
