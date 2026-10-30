import sys
import base64
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_servers.common.errors import AuthorizationError, PortalIntegrationError
from mcp_servers.citizen import tools
from mcp_servers.citizen import server
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
    assert (
        "'**mobile number** (10 digits): please enter your mobile number.'"
        in instructions
    )
    assert "keep the entire interaction in this chat" in instructions
    assert "never open a website, browser, or webview" in instructions
    assert (
        "'**otp verification**\notp sent successfully. please enter "
        "the 6-digit otp you received.'"
        in instructions
    )
    assert "end the turn and wait" in instructions
    assert "do not call verify_citizen_otp in the same turn" in instructions
    assert "after every tool call, send a user-facing follow-up" in instructions
    assert instructions.index("request_citizen_otp") < instructions.index(
        "verify_citizen_otp"
    )
    assert "an opaque session_ref, not the portal credential" in instructions
    assert "never display it" in instructions
    assert "verification automatically creates or resumes" in instructions
    assert "never repeat or reveal the otp" in instructions
    assert "application_exists" in instructions
    assert "without calling check_my_application again" in instructions
    assert "ask exactly one missing field per message" in instructions
    assert "save only that field immediately" in instructions
    assert "never ask again for saved answers" in instructions
    assert "applicant name; date of birth (dd/mm/yyyy); gender; marital status" in instructions
    assert "return the actual application number" in instructions
    create_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "create_or_resume_application_draft"
    )
    create_description = " ".join(create_tool.description.lower().split())
    assert "do not call check_my_application first" in create_description
    save_tool = next(
        tool for tool in mcp._tool_manager.list_tools()
        if tool.name == "save_application_draft"
    )
    assert "save one or more answers" in save_tool.description.lower()


def test_citizen_otp_conversation_waits_then_verifies_before_continuing(
    monkeypatch,
):
    calls = []

    def request_otp(mobile):
        calls.append(("request", mobile))
        return {"status": "otp_sent", "expires_in": 300}

    def verify_otp(mobile, otp):
        calls.append(("verify", mobile, otp))
        return {
            "session_token": "private-citizen-session",
            "expires_in": 7200,
            "session_expires_in": 7200,
        }

    def create_draft(session_token, **details):
        calls.append(("draft", session_token, details))
        return {
            "draft_ref": "draft-123",
            "details": {},
            "document_status": "PENDING_UPLOAD",
        }

    monkeypatch.setattr(server, "send_citizen_otp", request_otp)
    monkeypatch.setattr(server, "verify_citizen_session_otp", verify_otp)
    monkeypatch.setattr(server, "create_citizen_draft", create_draft)

    request_result = server.request_citizen_otp("5551234567")

    assert request_result["assistant_message"] == (
        "**OTP verification**\n"
        "OTP sent successfully. Please enter the 6-digit OTP you received."
    )
    assert request_result["next_action"] == "wait_for_citizen_otp"
    assert calls == [("request", "5551234567")]

    verification_result = server.verify_citizen_otp("5551234567", "123456")

    assert verification_result["verified"] is True
    assert verification_result["draft"]["draft_ref"] == "draft-123"
    assert "session_token" not in verification_result
    session_ref = verification_result["session_ref"]
    assert session_ref != "private-citizen-session"
    assert verification_result["assistant_message"] == (
        "Your mobile number is verified and your draft is ready. "
        "Continue with the first missing application detail."
    )
    assert calls[-1] == (
        "draft",
        "private-citizen-session",
        {},
    )

    saved = []
    monkeypatch.setattr(
        server,
        "save_citizen_draft",
        lambda draft_ref, token, **details: saved.append(
            (draft_ref, token, details)
        ) or {"draft_ref": draft_ref, "details": details},
    )
    saved_result = server.save_application_draft(
        "draft-123",
        session_ref,
        applicant_name="Test Citizen",
    )
    assert saved_result["next_action"] == "collect_next_application_detail"
    assert saved == [
        ("draft-123", "private-citizen-session", {"applicant_name": "Test Citizen"})
    ]


def test_invalid_or_expired_citizen_otp_requests_retry_without_continuing(
    monkeypatch,
):
    def reject_otp(_mobile, _otp):
        raise PortalIntegrationError(
            "Portal request failed (401): Invalid or expired OTP."
        )

    monkeypatch.setattr(server, "verify_citizen_session_otp", reject_otp)

    result = server.verify_citizen_otp("5551234567", "000000")

    assert result == {
        "verified": False,
        "assistant_message": "That OTP is invalid or expired. Please try again.",
        "next_action": "wait_for_citizen_otp",
    }


def test_citizen_otp_network_errors_are_not_reported_as_verification(
    monkeypatch,
):
    def fail_verification(_mobile, _otp):
        raise PortalIntegrationError("The portal could not complete the request.")

    monkeypatch.setattr(server, "verify_citizen_session_otp", fail_verification)

    with pytest.raises(PortalIntegrationError, match="could not complete"):
        server.verify_citizen_otp("5551234567", "123456")


def test_citizen_otp_verification_without_session_token_is_not_success(
    monkeypatch,
):
    monkeypatch.setattr(
        server,
        "verify_citizen_session_otp",
        lambda _mobile, _otp: {"status": "verified"},
    )

    with pytest.raises(PortalIntegrationError, match="no citizen session token"):
        server.verify_citizen_otp("5551234567", "123456")


def test_failed_citizen_otp_request_does_not_prompt_for_a_code(monkeypatch):
    monkeypatch.setattr(
        server,
        "send_citizen_otp",
        lambda _mobile: {"error": "Too many OTP requests."},
    )

    result = server.request_citizen_otp("5551234567")

    assert result == {"error": "Too many OTP requests."}


def test_full_citizen_application_flow_keeps_token_private(monkeypatch):
    private_token = "portal-session-token"
    calls = []

    monkeypatch.setattr(
        server,
        "send_citizen_otp",
        lambda mobile: {"status": "otp_sent", "expires_in": 300},
    )
    monkeypatch.setattr(
        server,
        "verify_citizen_session_otp",
        lambda mobile, otp: {
            "session_token": private_token,
            "expires_in": 3600,
            "session_expires_in": 7200,
        },
    )

    def create_draft(token, **details):
        calls.append(("create", token, details))
        return {
            "draft_ref": "draft-safe-ref",
            "details": {},
            "document_status": "PENDING_UPLOAD",
        }

    def save_draft(draft_ref, token, **details):
        calls.append(("save", draft_ref, token, details))
        result_details = dict(details)
        if "bank_account" in result_details:
            result_details["bank_account"] = "123456789012"
        return {"draft_ref": draft_ref, "details": result_details}

    def attach_document(draft_ref, filename, content, token):
        calls.append(("attach", draft_ref, filename, token))
        return {"draft_ref": draft_ref, "document_attached": True}

    def validate_document(draft_ref, token):
        calls.append(("validate", draft_ref, token))
        return {"draft_ref": draft_ref, "document_status": "VALID"}

    def read_draft(draft_ref, token):
        calls.append(("read", draft_ref, token))
        return {
            "draft_ref": draft_ref,
            "details": {
                "applicant_name": "Test Citizen",
                "dob": "01/01/1960",
                "village": "Example Village",
                "block": "Example Block",
                "bank_account": "123456789012",
                "ifsc": "SBIN0003077",
            },
            "document_status": "VALID",
            "document_attached": True,
        }

    def submit_draft(**kwargs):
        calls.append(("submit", kwargs))
        return {
            "application_number": "SSP12345678",
            "status": "PENDING",
        }

    monkeypatch.setattr(server, "create_citizen_draft", create_draft)
    monkeypatch.setattr(server, "save_citizen_draft", save_draft)
    monkeypatch.setattr(server, "attach_citizen_age_proof", attach_document)
    monkeypatch.setattr(server, "validate_citizen_age_proof", validate_document)
    monkeypatch.setattr(server, "read_citizen_draft", read_draft)
    monkeypatch.setattr(server, "create_pension_application", submit_draft)

    otp_prompt = server.request_citizen_otp("5551234567")
    assert otp_prompt["next_action"] == "wait_for_citizen_otp"
    verified = server.verify_citizen_otp("5551234567", "123456")
    session_ref = verified["session_ref"]
    assert verified["draft"]["draft_ref"] == "draft-safe-ref"
    assert private_token not in repr(verified)
    assert "session_token" not in verified
    assert calls == [("create", private_token, {})]

    for field in (
        {"applicant_name": "Test Citizen"},
        {"date_of_birth": "01/01/1960"},
        {"gender": "Female"},
        {"marital_status": "Widow"},
        {"village": "Example Village"},
        {"block": "Example Block"},
        {"bank_account": "123456789012"},
        {"ifsc": "SBIN0003077"},
    ):
        saved = server.save_application_draft(
            "draft-safe-ref",
            session_ref,
            **field,
        )
        assert saved["next_action"] == "collect_next_application_detail"
        assert private_token not in repr(saved)
        assert "123456789012" not in repr(saved)

    attached = server.attach_age_proof(
        "draft-safe-ref",
        "age-proof.pdf",
        base64.b64encode(b"%PDF-test").decode("ascii"),
        session_ref,
    )
    assert attached["next_action"] == "validate_age_proof"
    validated = server.validate_age_proof("draft-safe-ref", session_ref)
    assert validated["document_status"] == "VALID"
    assert validated["next_action"] == "review_and_confirm"

    preview = server.get_application_draft("draft-safe-ref", session_ref)
    assert "SSP" not in preview["assistant_message"]
    assert "123456789012" not in repr(preview)
    assert "********9012" in preview["assistant_message"]
    assert "VALID" in preview["assistant_message"]

    not_confirmed = server.submit_pension_application(
        "draft-safe-ref", False, True, session_ref
    )
    assert not_confirmed["submitted"] is False
    assert not any(call[0] == "submit" for call in calls)

    submitted = server.submit_pension_application(
        "draft-safe-ref", True, True, session_ref
    )
    assert submitted["application_number"] == "SSP12345678"
    assert submitted["status"] == "PENDING"
    assert "SSP12345678" in submitted["assistant_message"]
    assert calls[-1] == (
        "submit",
        {
            "draft_ref": "draft-safe-ref",
            "session_token": private_token,
            "declaration_confirmed": True,
            "summary_confirmed": True,
        },
    )


def test_otp_verification_retries_draft_creation_without_reverifying(monkeypatch):
    verified = []
    draft_calls = []

    monkeypatch.setattr(
        server,
        "verify_citizen_session_otp",
        lambda mobile, otp: verified.append((mobile, otp)) or {
            "session_token": "private-token",
            "expires_in": 600,
            "session_expires_in": 7200,
        },
    )
    monkeypatch.setattr(
        server,
        "create_citizen_draft",
        lambda token, **details: (
            draft_calls.append((token, details))
            or (_ for _ in ()).throw(PortalIntegrationError("temporary network error"))
        ),
    )

    first = server.verify_citizen_otp("5551234567", "123456")
    assert first["verified"] is True
    assert first["draft_ready"] is False
    assert first["next_action"] == "retry_create_or_resume_application_draft"

    monkeypatch.setattr(
        server,
        "create_citizen_draft",
        lambda token, **details: {
            "draft_ref": "draft-after-retry",
            "details": {},
            "document_status": "PENDING_UPLOAD",
        },
    )
    retried = server.create_or_resume_application_draft(first["session_ref"])

    assert retried["draft_ref"] == "draft-after-retry"
    assert verified == [("5551234567", "123456")]
    assert draft_calls == [("private-token", {})]


def test_duplicate_application_stops_new_draft_and_discards_session(monkeypatch):
    monkeypatch.setattr(
        server,
        "verify_citizen_session_otp",
        lambda _mobile, _otp: {
            "session_token": "private-token",
            "expires_in": 600,
            "session_expires_in": 7200,
        },
    )
    monkeypatch.setattr(
        server,
        "create_citizen_draft",
        lambda _token, **_details: {
            "application_exists": True,
            "application_number": "SSP87654321",
            "status": "PENDING",
        },
    )

    result = server.verify_citizen_otp("5551234567", "123456")

    assert result["application_exists"] is True
    assert result["application_number"] == "SSP87654321"
    assert "session_ref" not in result
    assert "session_token" not in result


def test_expired_citizen_session_reference_is_rejected(monkeypatch):
    reference = "opaque-session-reference"
    monkeypatch.setitem(
        server._citizen_sessions,
        reference,
        ("private-token", server.time.monotonic() - 1),
    )

    with pytest.raises(AuthorizationError, match="session expired"):
        server.check_my_application(reference)


def test_portal_rejecting_session_invalidates_opaque_reference(monkeypatch):
    reference = "opaque-session-reference"
    monkeypatch.setitem(
        server._citizen_sessions,
        reference,
        ("private-token", server.time.monotonic() + 60),
    )
    monkeypatch.setattr(
        server,
        "check_application_status",
        lambda _token: (_ for _ in ()).throw(
            PortalIntegrationError("Portal request failed (401): expired")
        ),
    )

    with pytest.raises(AuthorizationError, match="session expired"):
        server.check_my_application(reference)
    with pytest.raises(AuthorizationError, match="unavailable"):
        server.check_my_application(reference)


def test_invalid_application_fields_are_rejected_before_save(monkeypatch):
    monkeypatch.setattr(
        server,
        "save_citizen_draft",
        lambda *_args, **_kwargs: pytest.fail("invalid answer must not be saved"),
    )
    reference = "opaque-session-reference"
    monkeypatch.setitem(
        server._citizen_sessions,
        reference,
        ("private-token", server.time.monotonic() + 60),
    )

    invalid_dob = server.save_application_draft(
        "draft", reference, date_of_birth="1960-01-01"
    )
    invalid_ifsc = server.save_application_draft(
        "draft", reference, ifsc="invalid"
    )

    assert invalid_dob["saved"] is False
    assert invalid_dob["next_action"] == "retry_date_of_birth"
    assert invalid_ifsc["saved"] is False
    assert invalid_ifsc["next_action"] == "retry_ifsc"


def test_submission_requires_real_application_number_and_status(monkeypatch):
    reference = "opaque-session-reference"
    monkeypatch.setitem(
        server._citizen_sessions,
        reference,
        ("private-token", server.time.monotonic() + 60),
    )
    monkeypatch.setattr(
        server,
        "create_pension_application",
        lambda **_kwargs: {"status": "PENDING"},
    )

    with pytest.raises(PortalIntegrationError, match="application number"):
        server.submit_pension_application("draft", True, True, reference)


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
    assert "otp verification is mandatory" in server_instructions
    assert "date of birth (dd/mm/yyyy)" in server_instructions


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
        "session_ref",
    }
    assert "never display or repeat it" in properties["session_ref"]["description"]


def test_all_citizen_personal_data_tools_require_a_session_token():
    public_tools = {
        "pension_scheme_information",
        "request_citizen_otp",
        "verify_citizen_otp",
    }

    for tool in mcp._tool_manager.list_tools():
        if tool.name not in public_tools:
            assert "session_ref" in tool.parameters["required"]


def test_application_slip_tool_returns_downloadable_pdf_attachment(monkeypatch):
    from mcp.types import BlobResourceContents, EmbeddedResource
    from mcp_servers.citizen import server

    monkeypatch.setattr(
        server,
        "download_application_slip",
        lambda _token: (b"%PDF-test", 'attachment; filename="SSP12345678.pdf"'),
    )
    monkeypatch.setattr(
        server,
        "_citizen_call",
        lambda _session_ref, operation: operation("citizen-session"),
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