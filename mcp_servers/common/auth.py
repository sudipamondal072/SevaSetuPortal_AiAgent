from functools import lru_cache
from typing import Literal

import jwt
import requests
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from mcp_servers.common.errors import AuthorizationError
from mcp_servers.common.config import PORTAL_BASE_URL, PORTAL_ISSUER_URL


JWKS_URL = f"{PORTAL_BASE_URL}/.well-known/jwks.json"


@lru_cache(maxsize=1)
def get_jwks_client():
    return PyJWKClient(JWKS_URL)


def verify_portal_token(
    token: str,
    expected_audience: str,
) -> dict:
    """Verify a portal-issued RS256 token and return its claims."""

    if not token:
        raise AuthorizationError("Missing access token.")

    try:
        signing_key = get_jwks_client().get_signing_key_from_jwt(token)

        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=[PORTAL_ISSUER_URL, PORTAL_ISSUER_URL.rstrip("/")],
            audience=expected_audience,
            options={
                "require": ["exp", "iss", "aud", "sub"],
            },
        )
    except (jwt.PyJWTError, requests.RequestException) as exc:
        # Do not return the token or cryptographic details to the caller.
        raise AuthorizationError(
            "Invalid, expired, or incorrectly scoped token."
        ) from exc

    if not isinstance(claims.get("sub"), str):
        raise AuthorizationError("Invalid token subject.")

    return claims


def require_scope(claims: dict, required_scope: str) -> None:
    raw_scope = claims.get("scope", "")
    scopes = raw_scope.split() if isinstance(raw_scope, str) else raw_scope

    if required_scope not in scopes:
        raise AuthorizationError("Required scope is missing.")


def require_role(claims: dict, expected_role: str) -> None:
    if claims.get("role") != expected_role:
        raise AuthorizationError("This action is not permitted for your role.")


class PortalTokenVerifier:
    """Adapt portal-issued tokens to the MCP SDK bearer-auth interface."""

    def __init__(self, audience: str, role: Literal["citizen", "officer"]):
        self.audience = audience
        self.role = role

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = verify_portal_token(token, self.audience)
            require_role(claims, self.role)
            require_scope(claims, self.role)
        except AuthorizationError:
            return None

        raw_scope = claims.get("scope", "")
        scopes = raw_scope.split() if isinstance(raw_scope, str) else raw_scope
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", claims["sub"]),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=self.audience,
            subject=claims["sub"],
            claims=claims,
        )