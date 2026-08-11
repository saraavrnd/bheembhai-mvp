# ADR-011: Pluggable object storage provider

**Status:** Accepted · **Date:** 2026-08-11 · **Deciders:** Saraav

## Context

ADR-005 selected S3 for execution artifacts, and the current design hardcodes S3 across every
layer: `steps.artifact_s3_key` in the schema, boto3 S3 client calls in the Engine Service, S3
pre-signed URLs in the Platform API's file endpoint, and S3-specific IAM policies for Fargate
tasks. This couples the entire artifact storage boundary to one cloud vendor.

BEEM-24's deployment target is AWS, where S3 is the right choice. But the product will be
deployed in other environments — Azure (Blob Storage), on-premises (MinIO, local filesystem),
or other clouds (GCS) — and each has different SDKs, URL schemes, and access patterns.
Swapping storage in the current design ripples through every service.

The boundary is narrow and identical across providers: upload a blob at a key, download a blob
at a key, generate a temporary access URL, list blobs under a prefix. Everything the rest of
the system needs maps to exactly these four operations regardless of the backing store.

## Decision

**Introduce a pluggable `ObjectStorage` protocol.** A Python `Protocol` class defines the
storage contract. One implementation exists per deployment; which one is selected by config at
startup.

```python
from dataclasses import dataclass
from typing import Protocol, AsyncIterator

@dataclass
class StoredObject:
    key: str
    size_bytes: int
    content_type: str | None

@dataclass
class PresignedUrl:
    url: str
    expires_in_seconds: int

class ObjectStorage(Protocol):
    """Pluggable artifact storage. One implementation per deployment."""
    backend_name: str                          # "s3", "azure_blob", "minio", "local"

    async def put(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        """Upload an object at key. Overwrites if exists."""
        ...

    async def get(self, key: str) -> bytes | None:
        """Download an object. Returns None if not found."""
        ...

    async def presigned_get_url(self, key: str, expires_in_seconds: int = 300) -> PresignedUrl:
        """Generate a temporary download URL for the file viewer."""
        ...

    async def list(self, prefix: str) -> AsyncIterator[StoredObject]:
        """List objects under a prefix (for artifact file listings in the UI)."""
        ...
```

Concrete implementations:

| Provider | Upload | Download | Pre-signed URL | Backend |
|----------|--------|----------|----------------|---------|
| `S3Storage` | `boto3.put_object` | `boto3.get_object` | `boto3.generate_presigned_url('get_object')` | S3 |
| `AzureBlobStorage` | `azure-storage-blob.upload_blob` | `azure-storage-blob.download_blob` | `generate_blob_sas` with read permission | Azure Blob Storage |
| `MinioStorage` | `minio.put_object` | `minio.get_object` | `minio.presigned_get_object` | MinIO (S3-compatible, on-prem) |
| `LocalStorage` | Write to filesystem path | Read from filesystem path | `file://` URL (dev only) | Local filesystem |

**Config-driven selection** (environment):

```yaml
storage_backend: s3
storage:
  s3:
    bucket: bheembhai-artifacts
    region: us-east-1
  azure_blob:
    connection_string: "${AZURE_STORAGE_CONNECTION_STRING}"
    container: bheembhai-artifacts
  minio:
    endpoint: http://minio:9000
    access_key: "${MINIO_ACCESS_KEY}"
    secret_key: "${MINIO_SECRET_KEY}"
    bucket: bheembhai-artifacts
    secure: false
  local:
    base_path: /var/bheembhai/artifacts
```

On startup, the app reads `storage_backend`, instantiates the matching class, and registers it
as a dependency. Every service that touches artifacts calls `storage.put(key, data)` /
`storage.get(key)` / `storage.presigned_get_url(key)` — provider-agnostic.

**Schema change:** `steps.artifact_s3_key` → `steps.artifact_storage_key TEXT` — just the
opaque key path (e.g. `runs/<run_id>/<step_id>/<attempt_no>/`). The backend prefix (S3 bucket
name, Azure container, local base path) is provider config, not stored per-row.

**Fargate task IAM:** The task role is scoped to the specific storage backend. For S3, the
policy grants `s3:PutObject` on the bucket. For Azure, the agent uses `DefaultAzureCredential`
with workload identity. The backend's `Runtime.launch()` receives the storage config as env
vars but does not hardcode *which* SDK the agent uses — the agent container includes all three
SDKs and picks based on `BB_STORAGE_BACKEND`.

**Agent container:** The agent writes results via the same plugin pattern — `run_skill.sh`
reads `BB_STORAGE_BACKEND` and calls the corresponding upload script
(`s3_upload.sh`, `azure_upload.sh`, etc.) or a Python script that dispatches on the config.
The result is always the same: a blob at `runs/<run_id>/<step_id>/<attempt_no>/bb_step_result.json`.

## Alternatives considered

- **Keep S3 as the only interface, use S3-compatible APIs for everything else (rejected):**
  MinIO is S3-compatible, but Azure Blob Storage and GCS are not. The S3 API is not a universal
  standard — abstracting at the storage API level means every non-AWS deployment runs an
  S3-compatibility layer (e.g., Azure has an S3-compatible endpoint but it's a separate product
  tier with different pricing and limitations). The protocol abstraction is lighter.
- **Abstract only at the agent level (rejected):** The agent writing to different backends is
  the easy part. The hard part is the Engine reading results and the Platform API generating
  access URLs — those run on the services, not the agent. The abstraction must cover all three.
- **Use a CDN/signed-URL service (rejected for MVP):** CloudFront signed URLs, Azure CDN, etc.
  Overengineered for an MVP where artifacts are small text files viewed by a single team. The
  `presigned_get_url` method on the protocol is the seam — we can swap in CDN URLs later
  without changing callers.

## Consequences

- **Easier:** Swapping storage backends becomes a config change (`storage_backend: azure_blob`)
  and one new file (`azure_blob_storage.py` ~100 lines), not a cross-service S3→Blob rewrite.
- **Easier:** Local dev uses `LocalStorage` (no LocalStack needed for artifact testing).
  Integration tests use `FakeObjectStorage` (in-memory dict).
- **Easier:** The `ObjectStorage` protocol is naturally testable — mock it in Engine tests
  without spinning up S3/LocalStack.
- **Harder:** The agent container now needs all three storage SDKs (boto3, azure-storage-blob,
  minio) or a dispatch script. Mitigated: the container image is built once and the SDKs are
  small. Alternatively, the agent can use a dedicated Python script in the agent image that
  imports the configured backend.
- **Harder:** Pre-signed URL semantics differ across providers (S3: query param signature,
  Azure: SAS token, MinIO: S3-compatible). The `PresignedUrl` return type abstracts this, but
  the UI must not assume the URL shape — it just renders a clickable link.
- **Note:** The `ObjectStorage` protocol is deliberately file/blob oriented (put/get/list/
  presigned_url). If the system later needs structured query access (e.g., "give me all
  diagnostics from runs in the last 7 days"), that belongs in a separate analytics/reporting
  layer — not in the artifact storage abstraction.
