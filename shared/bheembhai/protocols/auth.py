"""Pluggable authentication provider protocol (ADR-010)."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Identity:
    """A validated user identity from an auth provider."""
    external_id: str       # Provider-scoped stable ID (Cognito sub, Azure oid, Okta sub)
    email: str
    display_name: str
    provider: str          # "cognito", "azure_ad", "okta"
    raw_claims: dict[str, Any] = field(default_factory=dict)


class AuthProvider(Protocol):
    """Pluggable authentication. One implementation per deployment."""

    provider_name: str

    async def validate(self, token: str) -> Identity | None:
        """Validate a bearer token and return the identity. None if invalid."""
        ...

    async def jwks(self) -> dict[str, Any]:
        """Return the JWKS (JSON Web Key Set) for token verification."""
        ...
