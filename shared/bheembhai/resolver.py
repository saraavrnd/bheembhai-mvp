"""Credential resolver — resolves ProjectIntegration.credential_ref → live value.

Used by the engine before it launches a step container.  The resolved values are
kept in memory only; they are never logged, serialised, or returned to callers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bheembhai.models.project import ProjectIntegration

logger = logging.getLogger(__name__)

# ── Sentinel for missing / unresolvable credentials ────────────────
_UNRESOLVABLE = object()


@dataclass
class ResolvedIntegration:
    """An integration whose credential has been fetched from SecureStorage.

    The raw credential lives ONLY in this dataclass — never in the DB row.
    """

    integration_id: str
    type: str          # "github", "jira", …
    label: str
    config: dict
    credential: str     # <-- THE RAW SECRET — do not log
    credential_ref: str

    # Convenience aliases so calling code is self-documenting
    @property
    def api_key(self) -> str:
        return self.credential

    @property
    def token(self) -> str:
        return self.credential


async def resolve_credentials(
    integrations: list["ProjectIntegration"],
    secure_storage,
) -> list[ResolvedIntegration]:
    """Resolve every integration's credential_ref against SecureStorage.

    Integrations whose credentials cannot be resolved are silently dropped
    (logged at WARNING) rather than crashing the run.
    """
    resolved: list[ResolvedIntegration] = []

    for integ in integrations:
        cred = await secure_storage.get(integ.credential_ref)
        if cred is None:
            logger.warning(
                "Integration %s (%s/%s): credential not found at ref %s — skipped",
                integ.id, integ.type, integ.label, integ.credential_ref,
            )
            continue

        resolved.append(ResolvedIntegration(
            integration_id=str(integ.id),
            type=integ.type,
            label=integ.label,
            config=integ.config or {},
            credential=cred.value,
            credential_ref=integ.credential_ref,
        ))

    return resolved


def mask_credential(value: str, show: int = 4) -> str:
    """Return a safe-for-logging version of a credential.

    ``mask_credential("ghp_abc123def456")`` → ``"ghp_****f456"``
    """
    if len(value) <= show * 2:
        return "*" * len(value)
    return f"{value[:show]}****{value[-show:]}"
