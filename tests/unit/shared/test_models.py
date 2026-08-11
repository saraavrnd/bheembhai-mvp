"""Trivial unit test — proves the test harness works (walking skeleton)."""

from bheembhai.models.user import User


def test_user_creation():
    """A User can be instantiated with required fields."""
    user = User(
        external_id="sub-123",
        auth_provider="cognito",
        email="test@example.com",
        display_name="Test User",
    )
    assert user.external_id == "sub-123"
    assert user.auth_provider == "cognito"
    assert user.email == "test@example.com"
    assert user.display_name == "Test User"
