"""Cognito auth provider — first AuthProvider implementation (ADR-010)."""

from typing import Any

import httpx
import jwt

from bheembhai.protocols.auth import Identity


class CognitoProvider:
    """Validates Cognito-issued JWTs against the User Pool."""

    provider_name = "cognito"

    def __init__(self, region: str, user_pool_id: str, client_id: str) -> None:
        self.region = region
        self.user_pool_id = user_pool_id
        self.client_id = client_id
        self._issuer = (
            f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        )
        self._jwks_url = f"{self._issuer}/.well-known/jwks.json"
        self._jwks_client = jwt.PyJWKClient(self._jwks_url)

    async def validate(self, token: str) -> Identity | None:
        try:
            print(f"  AUTH  CognitoProvider.validate: fetching JWKS from {self._jwks_url}", flush=True)
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            print("  AUTH  CognitoProvider.validate: JWKS key obtained, decoding JWT...", flush=True)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self.client_id,
                options={"verify_exp": True},
            )
            print(f"  AUTH  CognitoProvider.validate: JWT valid — sub={claims.get('sub','?')} email={claims.get('email','?')}", flush=True)
            return Identity(
                external_id=claims["sub"],
                email=claims.get("email", ""),
                display_name=claims.get("name", claims.get("email", "").split("@")[0]),
                provider="cognito",
                raw_claims=claims,
            )
        except jwt.ExpiredSignatureError:
            print("  AUTH  CognitoProvider.validate: TOKEN EXPIRED", flush=True)
            return None
        except jwt.InvalidTokenError as e:
            print(f"  AUTH  CognitoProvider.validate: INVALID TOKEN — {e}", flush=True)
            return None
        except Exception as e:  # noqa: BLE001 — JWKS/network boundary: any failure = invalid token
            print(f"  AUTH  CognitoProvider.validate: UNEXPECTED ERROR — {type(e).__name__}: {e}", flush=True)
            return None

    async def jwks(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(self._jwks_url)
            resp.raise_for_status()
            return resp.json()
