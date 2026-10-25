import pytest

from mcp_servers.common.errors import PortalIntegrationError
from mcp_servers.common.portal_client import PortalClient


def test_rejects_invalid_api_path():
    client = PortalClient("http://localhost:8000")

    with pytest.raises(ValueError):
        client.request(
            "GET",
            "//untrusted.example/path",
            "not-a-real-token",
        )


def test_sends_portal_bearer_token(monkeypatch):
    import requests

    captured = {}

    class Response:
        is_redirect = False
        status_code = 200

        def json(self):
            return {"ok": True}

        def raise_for_status(self):
            pass

    def fake_request(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "request", fake_request)
    result = PortalClient("http://portal").request(
        "GET",
        "/api/agent/citizen/application",
        "test-access-token",
    )

    assert result == {"ok": True}
    assert captured["headers"]["Authorization"] == "Bearer test-access-token"


def test_returns_portal_validation_message(monkeypatch):
    import requests

    class Response:
        is_redirect = False
        status_code = 400

        def json(self):
            return {
                "error": "Application validation failed.",
                "validation_errors": ["applicant must be 60 years of age or above"],
            }

    monkeypatch.setattr(requests, "request", lambda **kwargs: Response())

    with pytest.raises(PortalIntegrationError, match="60 years"):
        PortalClient("http://portal").request(
            "POST",
            "/api/agent/citizen/applications",
            "test-access-token",
            json_body={},
        )


def test_downloads_pdf_with_portal_bearer_token(monkeypatch):
    import requests

    captured = {}

    class Response:
        is_redirect = False
        status_code = 200
        content = b"%PDF-test"
        headers = {
            "Content-Type": "application/pdf",
            "Content-Disposition": 'attachment; filename="SSP12345678.pdf"',
        }

    def fake_request(**kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "request", fake_request)

    content, disposition = PortalClient("http://portal").request_bytes(
        "GET",
        "/api/agent/citizen/application/slip",
        "test-access-token",
    )

    assert content == b"%PDF-test"
    assert disposition == 'attachment; filename="SSP12345678.pdf"'
    assert captured["headers"]["Authorization"] == "Bearer test-access-token"
    assert captured["headers"]["Accept"] == "application/pdf"


def test_request_timing_log_redacts_draft_reference_and_request_data(
    monkeypatch, caplog
):
    import logging
    import requests

    class Response:
        is_redirect = False
        status_code = 200

        def json(self):
            return {"ok": True}

    monkeypatch.setattr(requests, "request", lambda **_kwargs: Response())

    with caplog.at_level(logging.INFO, logger="mcp_servers.common.portal_client"):
        PortalClient("http://portal").request(
            "PATCH",
            "/api/agent/citizen/application-drafts/private-draft-ref",
            "private-access-token",
            json_body={"bank_account": "123456789012"},
        )

    assert "mcp_portal_http" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "/application-drafts/<draft_ref>" in caplog.text
    assert "private-draft-ref" not in caplog.text
    assert "private-access-token" not in caplog.text
    assert "123456789012" not in caplog.text