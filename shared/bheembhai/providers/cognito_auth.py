"""Cognito auth service — AWS SDK wrapper for login / refresh / signup.

Separate from CognitoProvider (which validates JWTs locally).
This class talks to Cognito over the wire via boto3.
"""

import json
import logging
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────


@dataclass
class AuthTokens:
    """Tokens returned by a successful Cognito auth call."""

    id_token: str
    access_token: str
    refresh_token: str
    expires_in: int


@dataclass
class AuthError:
    """Structured auth error — safe to return to the caller."""

    code: str       # "InvalidCredentials", "UserNotConfirmed", etc.
    message: str    # Human-readable


# ── Exceptions ────────────────────────────────────────────────


class CognitoAuthError(Exception):
    """Cognito returned an error we can't map to a known AuthError code."""


# ── Service ───────────────────────────────────────────────────


class CognitoAuthService:
    """Thin wrapper around boto3 CognitoIdentityProvider for login flows.

    All methods are sync (boto3 is sync by default). Callers should
    run these in a thread-pool executor if the route is async.
    """

    def __init__(
        self,
        region: str,
        user_pool_id: str,
        client_id: str,
        *,
        endpoint_url: str | None = None,
    ) -> None:
        self.region = region
        self.user_pool_id = user_pool_id
        self.client_id = client_id

        client_kwargs: dict = {"region_name": region}
        if endpoint_url:
            client_kwargs["endpoint_url"] = endpoint_url

        self._client = boto3.client("cognito-idp", **client_kwargs)

    # ── Login ─────────────────────────────────────────────────

    def login(self, email: str, password: str) -> AuthTokens | AuthError:
        """Authenticate with USER_PASSWORD_AUTH flow.

        Returns AuthTokens on success, AuthError on failure.
        """
        try:
            resp = self._client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={
                    "USERNAME": email,
                    "PASSWORD": password,
                },
            )
        except ClientError as exc:
            return self._map_client_error(exc)

        # ── Debug: log all top-level keys in the Cognito response ─
        print(f"  AUTH  Cognito response keys: {list(resp.keys())}", flush=True)
        for key in resp:
            val = resp[key]
            if isinstance(val, dict):
                print(f"  AUTH    {key}: {json.dumps(val, default=str)[:200]}", flush=True)
            else:
                print(f"  AUTH    {key}: {str(val)[:200]}", flush=True)

        # ── Check for challenge (NEW_PASSWORD_REQUIRED, MFA, etc.) ─
        challenge = resp.get("ChallengeName")
        if challenge:
            logger.warning(
                "Cognito challenge: %s (user=%s)", challenge, email,
            )
            return AuthError(
                code=challenge,
                message=f"Cognito requires additional action: {challenge}. "
                        "Use /api/auth/respond-to-challenge to complete authentication.",
            )

        result = resp.get("AuthenticationResult", {})
        id_token = result.get("IdToken", "")
        access_token = result.get("AccessToken", "")
        refresh_token = result.get("RefreshToken", "")

        if not id_token:
            logger.warning(
                "Cognito returned empty IdToken. Response keys: %s",
                list(resp.keys()),
            )
            return AuthError(
                code="EmptyToken",
                message="Cognito did not return an IdToken. The credentials may be correct "
                        "but the authentication flow requires additional steps.",
            )

        return AuthTokens(
            id_token=id_token,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=result.get("ExpiresIn", 3600),
        )

    # ── Refresh ───────────────────────────────────────────────

    def refresh(self, refresh_token: str) -> AuthTokens | AuthError:
        """Exchange a refresh token for new tokens."""
        try:
            resp = self._client.initiate_auth(
                ClientId=self.client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={
                    "REFRESH_TOKEN": refresh_token,
                },
            )
        except ClientError as exc:
            return self._map_client_error(exc)

        result = resp.get("AuthenticationResult", {})
        return AuthTokens(
            id_token=result.get("IdToken", ""),
            access_token=result.get("AccessToken", ""),
            refresh_token=result.get("RefreshToken", ""),
            expires_in=result.get("ExpiresIn", 3600),
        )

    # ── Signup ────────────────────────────────────────────────

    def signup(
        self, email: str, password: str, name: str
    ) -> dict | AuthError:
        """Register a new user in the Cognito User Pool.

        Returns the SignUp response dict on success.
        """
        try:
            resp = self._client.sign_up(
                ClientId=self.client_id,
                Username=email,
                Password=password,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "name", "Value": name},
                ],
            )
            return resp
        except ClientError as exc:
            return self._map_client_error(exc)

    # ── Confirm signup ────────────────────────────────────────

    def confirm_signup(self, email: str, code: str) -> bool | AuthError:
        """Confirm a user's registration with the verification code."""
        try:
            self._client.confirm_sign_up(
                ClientId=self.client_id,
                Username=email,
                ConfirmationCode=code,
            )
            return True
        except ClientError as exc:
            return self._map_client_error(exc)

    # ── User lookup ───────────────────────────────────────────

    def get_user(self, access_token: str) -> dict | AuthError:
        """Get user attributes from Cognito using an access token."""
        try:
            resp = self._client.get_user(AccessToken=access_token)
            attrs = {
                attr["Name"]: attr["Value"]
                for attr in resp.get("UserAttributes", [])
            }
            return {
                "username": resp.get("Username"),
                "attributes": attrs,
            }
        except ClientError as exc:
            return self._map_client_error(exc)

    # ── Error mapping ─────────────────────────────────────────

    def _map_client_error(self, exc: ClientError) -> AuthError:
        """Map a boto3 ClientError to a structured AuthError."""
        code = exc.response.get("Error", {}).get("Code", "UnknownError")
        message = exc.response.get("Error", {}).get("Message", str(exc))

        # Map Cognito error codes to user-friendly codes
        mapping: dict[str, str] = {
            "NotAuthorizedException": "InvalidCredentials",
            "UserNotFoundException": "InvalidCredentials",
            "PasswordResetRequiredException": "PasswordResetRequired",
            "UserNotConfirmedException": "UserNotConfirmed",
            "UsernameExistsException": "EmailAlreadyExists",
            "InvalidPasswordException": "WeakPassword",
            "CodeMismatchException": "InvalidCode",
            "ExpiredCodeException": "ExpiredCode",
            "LimitExceededException": "RateLimited",
            "TooManyRequestsException": "RateLimited",
            "TooManyFailedAttemptsException": "RateLimited",
        }

        mapped = mapping.get(code, code)
        logger.warning("Cognito auth error: code=%s mapped=%s", code, mapped)
        return AuthError(code=mapped, message=message)
