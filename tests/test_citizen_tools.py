import sys
import base64
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_servers.common.errors import AuthorizationError
from mcp_servers.citizen import tools
from mcp_servers.citizen.server import mcp


def test_withdraw_application_is_registered_in_citizen_mcp_server():
    registered_names = {tool.name for tool in mcp._tool_manager.list_tools()}

    assert "request_citizen_otp" in registered_names
    assert "verify_citizen_otp" in registered_names
    assert "withdraw_my_application" in registered_names
    assert "get_my_application_slip" in registered_names
    assert "create_or_resume_application_draft" in registered_names
    assert "save_application_draft" in registered_names
    assert "attach_age_proof" in registered_names
    assert "validate_age_proof" in registered_names
    assert "get_application_draft" in registered_names
    instructions = mcp.instructions.lower()
    assert "preflight" in instructions
    assert "without calling check_my_application again" in instructions
    assert "one save_application_draft call" in instructions
    create_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "create_or_resume_application_draft"
    )
    create_description = " ".join(create_tool.description.lower().split())
    assert "do not call check_my_application first" in create_description


def test_draft_document_tool_requests_actual_proof_file_attachment():
    registered_tools = mcp._tool_manager.list_tools()
    attach_tool = next(
        tool for tool in registered_tools if tool.name == "attach_age_proof"
    )
    properties = attach_tool.parameters["properties"]

    assert "file_base64" in properties
    description = properties["file_base64"]["description"].lower()
    assert "actual file bytes" in description
    assert "never infer" in description
    server_instructions = mcp.instructions.lower()
    assert "review_required" in server_instructions
    assert "masked summary" in server_instructions
    assert "otp verification is always required" in server_instructions


def test_submit_tool_requires_draft_and_both_confirmations():
    submit_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "submit_pension_application"
    )
    properties = submit_tool.parameters["properties"]

    assert set(properties) == {
        "draft_ref",
        "declaration_confirmed",
        "summary_confirmed",
        "session_token",
    }
    assert "never display or repeat it" in properties["session_token"]["description"]


def test_all_citizen_personal_data_tools_require_a_session_token():
    public_tools = {
        "pension_scheme_information",
        "request_citizen_otp",
        "verify_citizen_otp",
    }

    for tool in mcp._tool_manager.list_tools():
        if tool.name not in public_tools:
            assert "session_token" in tool.parameters["required"]


def test_application_slip_tool_returns_downloadable_pdf_attachment(monkeypatch):
    from mcp.types import BlobResourceContents, EmbeddedResource
    from mcp_servers.citizen import server

    monkeypatch.setattr(
        server,
        "download_application_slip",
        lambda _token: (b"%PDF-test", 'attachment; filename="SSP12345678.pdf"'),
    )

    result = server.get_my_application_slip("citizen-session")

    assert "SSP12345678.pdf" in result.content[0].text
    attachment = result.content[1]
    assert isinstance(attachment, EmbeddedResource)
    assert isinstance(attachment.resource, BlobResourceContents)
    assert attachment.resource.mimeType == "application/pdf"
    assert base64.b64decode(attachment.resource.blob) == b"%PDF-test"


def test_withdraw_application_forwards_confirmation_and_session(monkeypatch):
    requests = []
    expected = {"application_id": 42, "status": "WITHDRAWN"}

    def request(self, method, path, token, *, params=None, json_body=None):
        requests.append((method, path, token, params, json_body))
        return expected

    monkeypatch.setattr(tools.PortalClient, "request", request)

    result = tools.withdraw_application(42, True, "citizen-session")

    assert result == expected
    assert requests == [
        (
            "POST",
            "/api/agent/citizen/applications/42/withdraw",
            "citizen-session",
            None,
            {"confirm": True},
        )
    ]


def test_withdraw_application_requires_a_verified_citizen_session():
    with pytest.raises(AuthorizationError):
        tools.withdraw_application(42, True, "")


def test_citizen_mcp_transport_is_public_but_officer_transport_remains_oauth():
    from mcp_servers.officer.server import mcp as officer_mcp

    assert mcp.settings.auth is None
    assert officer_mcp.settings.auth is not None