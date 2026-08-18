"""Shared user-resolution helper — used by all routers that need a local User row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from bheembhai.models.user import User
from bheembhai.protocols.auth import Identity
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def get_or_create_user(
    identity: Identity | None, db: AsyncSession
) -> User:
    """Find an existing user by external_id + provider, or create one.

    Every authenticated request creates a local ``User`` row on first visit so
    foreign keys (projects.owner_id, memberships.user_id) resolve cleanly.

    When ``identity`` is None (DEV_AUTH_BYPASS mode or unauthenticated), a
    hardcoded dev identity is used.
    """
    if identity is None:
        identity = Identity(
            external_id="dev-user",
            email="dev@bheembhai.local",
            display_name="Dev User",
            provider="dev",
        )

    result = await db.execute(
        select(User).where(
            User.external_id == identity.external_id,
            User.auth_provider == identity.provider,
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        # First user in the system becomes ADMIN; subsequent users get USER
        count_result = await db.execute(select(User).limit(1))
        is_first = count_result.scalar_one_or_none() is None
        user = User(
            external_id=identity.external_id,
            email=identity.email,
            display_name=identity.display_name,
            auth_provider=identity.provider,
            platform_role="ADMIN" if is_first else "USER",
        )
        db.add(user)
        await db.flush()

    return user
