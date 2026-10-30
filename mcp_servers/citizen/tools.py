import base64

from mcp_servers.common.errors import AuthorizationError
from mcp_servers.common.portal_client import PortalClient


def _access_token(session_token: str) -> str:
    if not isinstance(session_token, str) or not session_token.strip():
        raise AuthorizationError("Verify your mobile number before using this tool.")
    return session_token.strip()


def request_citizen_otp(mobile: str) -> dict:
    """Request a rate-limited one-time code for a citizen MCP session."""
    return PortalClient().request(
        "POST",
        "/api/agent/citizen/session/otp/request",
        json_body={"mobile": mobile},
    )


def verify_citizen_otp(mobile: str, otp: str) -> dict:
    """Verify an OTP and receive a short-lived citizen session credential."""
    return PortalClient().request(
        "POST",
        "/api/agent/citizen/session/otp/verify",
        json_body={"mobile": mobile, "otp": otp},
    )


def get_scheme_information() -> dict:
    """Read official, current scheme rules from the Sewa Setu portal."""
    return PortalClient().request("GET", "/api/agent/scheme")


def get_application_status(session_token: str) -> dict:
    """Check the authenticated citizen's application or saved draft."""
    return PortalClient().request(
        "GET",
        "/api/agent/citizen/application",
        _access_token(session_token),
    )


def create_or_resume_application_draft(session_token: str, **details: str) -> dict:
    """Create or resume the one draft owned by the authenticated citizen."""
    return PortalClient().request(
        "POST",
        "/api/agent/citizen/application-drafts",
        _access_token(session_token),
        json_body=details,
    )


def read_application_draft(draft_ref: str, session_token: str) -> dict:
    """Read saved draft details with the bank account masked."""
    return PortalClient().request(
        "GET",
        f"/api/agent/citizen/application-drafts/{draft_ref}",
        _access_token(session_token),
    )


def save_application_draft(
    draft_ref: str, session_token: str, **details: str
) -> dict:
    """Save or update application details in the citizen's own draft."""
    return PortalClient().request(
        "PATCH",
        f"/api/agent/citizen/application-drafts/{draft_ref}",
        _access_token(session_token),
        json_body=details,
    )


def attach_age_proof(
    draft_ref: str, filename: str, file_base64: str, session_token: str
) -> dict:
    """Attach an actual supported age-proof file to the citizen's saved draft."""
    return PortalClient().request(
        "POST",
        f"/api/agent/citizen/application-drafts/{draft_ref}/age-proof",
        _access_token(session_token),
        json_body={"filename": filename, "file_base64": file_base64},
    )


def validate_age_proof(draft_ref: str, session_token: str) -> dict:
    """Run backend file checks and report any required officer review."""
    return PortalClient().request(
        "POST",
        f"/api/agent/citizen/application-drafts/{draft_ref}/validate-age-proof",
        _access_token(session_token),
        json_body={},
    )


def get_application_slip(session_token: str) -> tuple[bytes, str]:
    """Download the authenticated citizen's latest application acknowledgment."""
    return PortalClient().request_bytes(
        "GET",
        "/api/agent/citizen/application/slip",
        _access_token(session_token),
    )


def withdraw_application(
    application_id: int, confirm: bool, session_token: str
) -> dict:
    """Withdraw the citizen's own pending application after explicit confirmation."""
    return PortalClient().request(
        "POST",
        f"/api/agent/citizen/applications/{application_id}/withdraw",
        _access_token(session_token),
        json_body={"confirm": confirm},
    )


def submit_pension_application(
    *,
    draft_ref: str,
    session_token: str,
    declaration_confirmed: bool,
    summary_confirmed: bool,
) -> dict:
    """Submit a fully validated draft after the citizen confirms its summary."""
    return PortalClient().request(
        "POST",
        f"/api/agent/citizen/application-drafts/{draft_ref}/submit",
        _access_token(session_token),
        json_body={
            "declaration_confirmed": declaration_confirmed,
            "summary_confirmed": summary_confirmed,
        },
    )


def base64_document(contents: bytes) -> str:
    """Encode file contents for MCP clients that need a local helper."""
    return base64.b64encode(contents).decode("ascii")
