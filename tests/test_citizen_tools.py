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
    }


def test_application_slip_tool_returns_downloadable_pdf_attachment(monkeypatch):
    from mcp.types import BlobResourceContents, EmbeddedResource
    from mcp_servers.citizen import server

    monkeypatch.setattr(
        server,
        "download_application_slip",
        lambda: (b"%PDF-test", 'attachment; filename="SSP12345678.pdf"'),
    )

    result = server.get_my_application_slip()

    assert "SSP12345678.pdf" in result.content[0].text
    attachment = result.content[1]
    assert isinstance(attachment, EmbeddedResource)
    assert isinstance(attachment.resource, BlobResourceContents)
    assert attachment.resource.mimeType == "application/pdf"
    assert base64.b64decode(attachment.resource.blob) == b"%PDF-test"


def test_withdraw_application_forwards_confirmation_and_token(monkeypatch):
    monkeypatch.setattr(tools, "_access_token", lambda: "citizen-token")
    requests = []
    expected = {"application_id": 42, "status": "WITHDRAWN"}

    def request(self, method, path, token, *, params=None, json_body=None):
        requests.append((method, path, token, params, json_body))
        return expected

    monkeypatch.setattr(tools.PortalClient, "request", request)

    result = tools.withdraw_application(42, True)

    assert result == expected
    assert requests == [
        (
            "POST",
            "/api/agent/citizen/applications/42/withdraw",
            "citizen-token",
            None,
            {"confirm": True},
        )
    ]


def test_withdraw_application_requires_citizen_authentication(monkeypatch):
    monkeypatch.setattr(tools, "get_access_token", lambda: None)

    with pytest.raises(AuthorizationError):
        tools.withdraw_application(42, True)