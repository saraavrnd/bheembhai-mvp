# ADR-010: Pluggable auth provider

**Status:** Accepted · **Date:** 2026-08-10 · **Deciders:** Saraav

## Context

ADR-002 and the initial data model baked Cognito into every layer: `users.cognito_sub` in the
schema, Cognito-specific JWT validation in the middleware, Cognito User Pool as the only auth
component in `architecture.md`. This couples the entire authentication boundary to one vendor.

BEEM-24's requirements call for AWS Cognito in this deployment, but the product will be deployed
in other environments that use Azure AD, Okta, or another OIDC provider. Swapping the provider
in the current design requires a schema migration (`cognito_sub` → something else), a middleware
rewrite, and config surgery — turning a deployment-level choice into a code change.

The backend's job at the auth boundary is narrow and identical across providers: verify a
bearer token, extract stable identity claims (sub, email, name), and map them to a local user
record. Everything the provider does before that — login UI, redirect dance, token exchange —
happens at the edge (ALB, Azure App Gateway, nginx). The backend should not care which
provider issued the token.

## Decision

**Introduce a pluggable `AuthProvider` protocol.** A Python `Protocol` class (not an abstract
base) defines the verification contract. One implementation exists per deployment; which one
is selected by a config value at startup.

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class Identity:
    external_id: str       # stable, provider-scoped user id (Cognito sub, Azure oid, Okta sub)
    email: str
    display_name: str
    provider: str          # "cognito", "azure_ad", "okta" — the configured provider name
    raw_claims: dict       # full validated claims, for provider-specific logic if needed

class AuthProvider(Protocol):
    """Pluggable identity verification. One implementation per deployment."""
    provider_name: str                          # matches the config key

    async def validate(self, token: str) -> Identity | None:
        """Verify token signature + expiry. Return normalized identity, or None if invalid."""
        ...

    async def jwks(self) -> dict:
        """Return JWKS for key rotation. Cached by middleware."""
        ...
```

Concrete implementations:

| Provider | Verification | JWKS source |
|----------|-------------|-------------|
| `CognitoProvider` | JWT + `cognito:sub` claim, validate against User Pool's JWKS | `https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json` |
| `AzureADProvider` | JWT + `oid` claim, validate against tenant's JWKS | `https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys` |
| `OktaProvider` | JWT + `sub` claim, validate against Okta domain's JWKS | `https://{domain}/oauth2/default/v1/keys` |

**Config-driven selection** (environment or `config/auth.yaml`):

```yaml
auth_provider: cognito
auth:
  cognito:
    region: us-east-1
    user_pool_id: us-east-1_xxxxx
    client_id: xxxxxxxxxx
  azure_ad:
    tenant_id: xxxxx-xxxx-xxxx
    client_id: xxxxx-xxxx-xxxx
```

On startup, the app reads `auth_provider`, instantiates the matching class, and registers it as a
FastAPI dependency. Every route calls `request.state.auth.validate(token)` — that call is
provider-agnostic.

**Schema change:** `users.cognito_sub` → `users.external_id TEXT UNIQUE NOT NULL` +
`users.auth_provider TEXT NOT NULL` (the provider name, e.g. `"cognito"`). The combination
`(external_id, auth_provider)` uniquely identifies a user. On a provider switch, existing users
are matched by email for continuity; new users get the new provider's external_id.

**Edge proxy still does the OAuth dance.** The provider's login flow (hosted UI, redirect,
callback) is handled by the ALB/gateway. The backend receives only a validated JWT in the
`Authorization` header (or extracted claims from `x-amzn-oidc-*` headers when behind ALB).
This keeps the provider plugin focused on token verification, not login orchestration.

## Alternatives considered

- **Abstract only at the ALB (rejected):** Rely on the ALB's OIDC support to normalize all
  providers into the same headers. Works for Cognito + Azure AD on AWS ALB, but breaks when:
  (a) the deployment doesn't have an ALB (local dev, single-EC2), (b) the ALB's OIDC support
  doesn't cover a provider's claim shape, or (c) you want to run integration tests without an
  ALB. The backend needs its own validation path.
- **Full OAuth flow in the backend (rejected for MVP):** Each plugin handles login, redirect,
  callback, and token exchange. More complete but overengineered — the edge proxy already does
  this. We can add login-flow methods to the protocol later if the edge-proxy assumption
  changes. The protocol is designed to grow those methods.
- **Federated identity table (rejected for MVP):** A `user_identities` table so one user can
  link multiple providers. Clean design, but zero MVP stories need cross-provider user linking.
  This is a seam for later — the `auth_provider` column is the natural anchor point to extract
  into a separate table when needed.

## Consequences

- **Easier:** Swapping auth providers becomes a config change (`auth_provider: azure_ad`) and
  one new file (`azure_ad_provider.py` ~80 lines), not a schema migration or middleware rewrite.
- **Easier:** Local dev without an ALB: configure the provider to validate JWT from the
  `Authorization` header directly, or add a `DevProvider` that trusts a hardcoded token.
- **Easier:** Integration tests can use a `FakeAuthProvider` that returns a fixed identity
  without any JWT validation or external calls.
- **Harder:** The `users` table now has a compound unique constraint `(external_id,
  auth_provider)` instead of a single `cognito_sub` column. Email-match logic on provider
  switch adds a small migration step.
- **Harder:** Auth config is provider-specific (Cognito needs `user_pool_id`, Azure needs
  `tenant_id`). The config schema must validate per-provider. Mitigated: config validation at
  startup with clear error messages.
- **Note:** The `AuthProvider` protocol is deliberately minimal (validate + jwks). If
  login-flow methods are needed later (e.g., `authorization_url()`), they can be added to the
  protocol without breaking existing implementations — they'd be optional methods that the
  backend calls only when present.
