"""Provider Protocol classes — pluggable boundaries (ADR-010, ADR-011, ADR-012)."""

from bheembhai.protocols.auth import AuthProvider, Identity
from bheembhai.protocols.secrets import Credential, SecureStorage
from bheembhai.protocols.storage import ObjectStorage, PresignedUrl, StoredObject

__all__ = [
    "AuthProvider",
    "Credential",
    "Identity",
    "ObjectStorage",
    "PresignedUrl",
    "SecureStorage",
    "StoredObject",
]
