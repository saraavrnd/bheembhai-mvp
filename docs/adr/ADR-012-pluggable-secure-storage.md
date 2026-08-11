# ADR-012: Pluggable secure storage provider

**Status:** Accepted · **Date:** 2026-08-11 · **Deciders:** Saraav

## Context

The data model stores `project_integrations.secret_arn` — an AWS Secrets Manager ARN — as the
reference to per-project credentials (GitHub tokens, Jira API tokens). The architecture doc,
components table, data flow, and NFRs all reference Secrets Manager as THE credential store.

This couples credential storage to one cloud vendor. When the product is deployed on Azure,
credentials belong in Azure Key Vault, not Secrets Manager. When deployed on-premises, they
may live in HashiCorp Vault or an encrypted environment variable. Hardcoding `secret_arn`
means every non-AWS deployment carries an AWS-shaped field it can't use.

The boundary is narrow: store a credential, retrieve a credential, rotate a credential
(optional). The rest of the system only needs the retrieval path — at runtime, the Engine
fetches the token at step launch time and injects it into the agent container's environment.
Neither the Platform API nor the Engine ever logs or persists the raw value.

## Decision

**Introduce a pluggable `SecureStorage` protocol.** A Python `Protocol` class defines the
credential retrieval contract. One implementation exists per deployment; which one is
selected by config at startup.

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class Credential:
    ref: str             # opaque, provider-specific reference (ARN, vault path, key name)
    value: str           # the retrieved secret value (never logged/persisted)
    provider: str        # "aws_secrets_manager", "azure_key_vault", "hashicorp_vault"

class SecureStorage(Protocol):
    """Pluggable credential storage. One implementation per deployment."""
    backend_name: str

    async def get(self, ref: str) -> Credential | None:
        """Retrieve a secret by its provider-specific reference. Returns None if not found."""
        ...

    async def put(self, ref: str, value: str, metadata: dict | None = None) -> str:
        """Store a secret. Returns the provider-specific reference (e.g., ARN)."""
        ...

    async def delete(self, ref: str) -> None:
        """Delete a secret. No-op if not found."""
        ...
```

Concrete implementations:

| Provider | `put` | `get` | `ref` shape |
|----------|-------|-------|-------------|
| `AWSSecretsManager` | `boto3.create_secret` | `boto3.get_secret_value` | `arn:aws:secretsmanager:...` |
| `AzureKeyVault` | `azure-keyvault-secrets.set_secret` | `azure-keyvault-secrets.get_secret` | `https://<vault>.vault.azure.net/secrets/<name>/<version>` |
| `HashiCorpVault` | `hvac.create_or_update_secret` | `hvac.secrets.kv.v2.read_secret_version` | `<engine>/data/<path>` |
| `EnvSecureStorage` | Writes to an encrypted config file on disk | Reads from encrypted config file | `env:<key_name>` (dev/local only) |

**Config-driven selection** (environment):

```yaml
secure_storage_backend: aws_secrets_manager
secure_storage:
  aws_secrets_manager:
    region: us-east-1
  azure_key_vault:
    vault_url: https://bheembhai.vault.azure.net
  hashicorp_vault:
    url: https://vault.internal:8200
    token_env: VAULT_TOKEN
  env:
    encrypted_config_path: /etc/bheembhai/secrets.enc
```

**Schema change:** `project_integrations.secret_arn` →
`project_integrations.credential_ref TEXT NOT NULL` — an opaque, provider-specific reference.
The `SecureStorage` backend interprets it. A deployment on Azure stores Key Vault URLs here;
a deployment on AWS stores Secrets Manager ARNs.

**Never store raw credentials.** The `credential_ref` is a pointer, not the value. The
Platform API calls `secure_storage.put(value)` at integration setup and discards the raw
token immediately — only the returned `ref` is persisted to Postgres. The Engine calls
`secure_storage.get(ref)` at step launch time and injects the value into the agent's
environment without logging it.

## Alternatives considered

- **Store credentials in Postgres encrypted at rest (rejected):** RDS encryption protects at
  rest, but anyone with DB access can SELECT the ciphertext and brute-force offline. A
  purpose-built secrets store (Secrets Manager, Key Vault, Vault) has access audit trails,
  rotation policies, and hardware-backed encryption that RDS doesn't. Credentials belong in
  a secrets store, not the main DB.
- **Keep `secret_arn` as a generic string, handle at the infra layer (rejected):** The
  "just rename the column" approach means the application code still calls `boto3` to
  resolve `secret_arn`. On Azure, there is no `boto3` and no ARN — the field is meaningless.
  The protocol abstraction means the application never imports the provider SDK directly.
- **Abstract only at the Engine level (rejected):** The Platform API also touches secrets
  (at integration setup time, to verify the token works before saving). Both services need
  the abstraction.

## Consequences

- **Easier:** Swapping secure storage becomes a config change and one new file (~80 lines),
  same pattern as AuthProvider (ADR-010) and ObjectStorage (ADR-011).
- **Easier:** Dev/testing uses `EnvSecureStorage` — credentials from env vars or an encrypted
  config file, no cloud service needed. Integration tests use `FakeSecureStorage`
  (in-memory dict).
- **Easier:** The `credential_ref` column is opaque — the application never parses it. A
  migration from AWS to Azure means running a script that calls `secure_storage.get(ref)` on
  the old backend and `secure_storage.put(value)` on the new one, then updating the refs.
- **Harder:** The Platform API must call `secure_storage.put()` synchronously at integration
  setup time. If the secrets backend is unreachable, integration setup fails with a clear
  error. Mitigated: this is a setup-time operation, not a runtime hot path.
- **Harder:** Secret rotation requires the backend to support it. Secrets Manager and Key
  Vault have native rotation; HashiCorp Vault and EnvSecureStorage don't. The protocol
  doesn't mandate a `rotate()` method for MVP — it can be added as an optional method later.
