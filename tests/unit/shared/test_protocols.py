"""Trivial unit test — proves protocol types work (walking skeleton)."""

from bheembhai.protocols.auth import Identity
from bheembhai.protocols.storage import PresignedUrl, StoredObject
from bheembhai.protocols.secrets import Credential


def test_identity_creation():
    """Identity dataclass is constructable."""
    identity = Identity(
        external_id="sub-abc",
        email="user@example.com",
        display_name="User",
        provider="cognito",
    )
    assert identity.provider == "cognito"
    assert identity.email == "user@example.com"


def test_stored_object_creation():
    """StoredObject dataclass is constructable."""
    obj = StoredObject(key="runs/1/out.json", data=b'{"ok": true}')
    assert obj.key == "runs/1/out.json"
    assert obj.data == b'{"ok": true}'


def test_presigned_url_creation():
    """PresignedUrl dataclass is constructable."""
    url = PresignedUrl(url="https://s3.example.com/bucket/key", expires_at=9999999999.0)
    assert url.url.startswith("https://")


def test_credential_creation():
    """Credential dataclass is constructable."""
    cred = Credential(ref="env:GH_TOKEN", value="ghp_test", provider="env")
    assert cred.ref == "env:GH_TOKEN"
    assert cred.provider == "env"
