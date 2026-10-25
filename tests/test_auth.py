import asyncio

import pytest

from mcp_servers.common.auth import (
    PortalTokenVerifier,
    require_role,
    require_scope,
)
from mcp_servers.common.errors import AuthorizationError


def test_citizen_cannot_use_officer_role():
    claims = {"role": "citizen"}

    with pytest.raises(AuthorizationError):
        require_role(claims, "officer")


def test_missing_scope_is_rejected():
    claims = {"scope": "citizen"}

    with pytest.raises(AuthorizationError):
        require_scope(claims, "officer")


def test_portal_verifier_rejects_other_role(monkeypatch):
    from mcp_servers.common import auth

    monkeypatch.setattr(
        auth,
        "verify_portal_token",
        lambda token, audience: {
            "sub": "officer:admin",
            "role": "officer",
            "scope": "officer",
            "exp": 123,
        },
    )

    verifier = PortalTokenVerifier("http://localhost:8001/mcp", "citizen")

    assert asyncio.run(verifier.verify_token("token")) is None


def test_portal_verifier_returns_verified_access_token(monkeypatch):
    from mcp_servers.common import auth

    claims = {
        "sub": "citizen:9000000001",
        "role": "citizen",
        "scope": "citizen",
        "client_id": "test-client",
        "exp": 123,
    }
    monkeypatch.setattr(
        auth,
        "verify_portal_token",
        lambda token, audience: claims,
    )

    verifier = PortalTokenVerifier("http://localhost:8001/mcp", "citizen")
    verified = asyncio.run(verifier.verify_token("token"))

    assert verified is not None
    assert verified.subject == "citizen:9000000001"
    assert verified.scopes == ["citizen"]
    assert verified.resource == "http://localhost:8001/mcp"