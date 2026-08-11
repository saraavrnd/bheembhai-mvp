"""Provider Protocol classes — pluggable boundaries (ADR-010, ADR-011, ADR-012)."""

from bheembhai.protocols.auth import AuthProvider, Identity
from bheembhai.protocols.storage import ObjectStorage, StoredObject, PresignedUrl
from bheembhai.protocols.secrets import SecureStorage, Credential

__all__ = [
    "AuthProvider",
    "Identity",
    "ObjectStorage",
    "StoredObject",
    "PresignedUrl",
    "SecureStorage",
    "Credential",
]
