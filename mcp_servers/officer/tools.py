from mcp.server.auth.middleware.auth_context import get_access_token

from mcp_servers.common.errors import AuthorizationError
from mcp_servers.common.portal_client import PortalClient


def _access_token() -> str:
    access = get_access_token()
    if access is None:
        raise AuthorizationError("Sign in as a block officer before using this tool.")
    return access.token


def list_pending_applications(limit: int = 20) -> dict:
    """List pending applications for the agent-enabled blocks."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/applications",
        _access_token(),
        params={"limit": limit},
    )


def list_approved_applications(limit: int = 20) -> dict:
    """List approved applications for the agent-enabled blocks."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/approved-applications",
        _access_token(),
        params={"limit": limit},
    )


def get_approved_applications(
    limit: int = 20,
    offset: int = 0,
    submitted_from: str = "",
    submitted_to: str = "",
    decision_from: str = "",
    decision_to: str = "",
    block: str = "",
) -> dict:
    """Retrieve approved applications and decision history in the officer's scope."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/approved-applications",
        _access_token(),
        params={
            "limit": limit,
            "offset": offset,
            "submitted_from": submitted_from,
            "submitted_to": submitted_to,
            "decision_from": decision_from,
            "decision_to": decision_to,
            "block": block,
        },
    )


def get_application(application_id: int) -> dict:
    """Read a case and audit trail within the agent-enabled block queue."""
    return PortalClient().request(
        "GET",
        f"/api/agent/officer/applications/{application_id}",
        _access_token(),
    )


def decide_application(application_id: int, decision: str, reason: str) -> dict:
    """Record an explicit officer decision with an audit reason."""
    return PortalClient().request(
        "POST",
        f"/api/agent/officer/applications/{application_id}/decision",
        _access_token(),
        json_body={"decision": decision, "reason": reason},
    )


def list_sla_risk_applications(
    days_threshold: int = 3,
    limit: int = 50,
) -> dict:
    """List pending cases within the given number of days of the statutory SLA."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/sla-risk",
        _access_token(),
        params={"days_threshold": days_threshold, "limit": limit},
    )


def list_deemed_approved_applications(days_back: int = 30, limit: int = 50) -> dict:
    """List applications that were approved by the statutory deemed-approval job."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/deemed-approved",
        _access_token(),
        params={"days_back": days_back, "limit": limit},
    )


def search_applications(query: str, status: str = "", limit: int = 20) -> dict:
    """Search applications in agent-enabled blocks by applicant or case details."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/search",
        _access_token(),
        params={"query": query, "status": status, "limit": limit},
    )


def get_applicant_history(mobile: str) -> dict:
    """Read an applicant's application history in agent-enabled blocks."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/applicant-history",
        _access_token(),
        params={"mobile": mobile},
    )


def check_duplicate_claims(application_id: int) -> dict:
    """Check for applications with matching bank, household, or mobile details."""
    return PortalClient().request(
        "GET",
        f"/api/agent/officer/applications/{application_id}/duplicates",
        _access_token(),
    )


def verify_bank_details(application_id: int) -> dict:
    """Validate bank field formats and flag known legacy IFSC prefixes."""
    return PortalClient().request(
        "GET",
        f"/api/agent/officer/applications/{application_id}/bank-verification",
        _access_token(),
    )


def get_application_document(application_id: int) -> dict:
    """Read age-proof metadata and its base64 file contents."""
    return PortalClient().request(
        "GET",
        f"/api/agent/officer/applications/{application_id}/document",
        _access_token(),
    )


def add_field_verification_note(
    application_id: int,
    note: str,
    verified_by: str,
) -> dict:
    """Append a field-verification note without making a decision."""
    return PortalClient().request(
        "POST",
        f"/api/agent/officer/applications/{application_id}/field-note",
        _access_token(),
        json_body={"note": note, "verified_by": verified_by},
    )


def batch_decide_applications(
    application_ids: list[int],
    decision: str,
    reason: str,
) -> dict:
    """Make one atomic decision for a list of pending applications."""
    return PortalClient().request(
        "POST",
        "/api/agent/officer/applications/batch-decision",
        _access_token(),
        json_body={
            "application_ids": application_ids,
            "decision": decision,
            "reason": reason,
        },
    )


def reassign_or_flag_case(
    application_id: int,
    flag_type: str,
    reason: str,
) -> dict:
    """Flag a pending case for follow-up and hold it back from deemed approval."""
    return PortalClient().request(
        "POST",
        f"/api/agent/officer/applications/{application_id}/flag",
        _access_token(),
        json_body={"flag_type": flag_type, "reason": reason},
    )


def get_queue_summary(block: str = "") -> dict:
    """Get pending, SLA-risk, and current-month decision counts."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/queue-summary",
        _access_token(),
        params={"block": block},
    )


def get_village_breakdown(block: str = "") -> dict:
    """Count pending and approved cases grouped by village."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/village-breakdown",
        _access_token(),
        params={"block": block},
    )


def list_age_proof_review_drafts() -> dict:
    """List citizen drafts waiting for authorized age-proof review."""
    return PortalClient().request(
        "GET",
        "/api/agent/officer/age-proof-review-drafts",
        _access_token(),
    )


def get_age_proof_review_document(draft_ref: str) -> dict:
    """Read a pending draft's supported age-proof file for manual inspection."""
    return PortalClient().request(
        "GET",
        f"/api/agent/officer/age-proof-review-drafts/{draft_ref}/document",
        _access_token(),
    )


def decide_age_proof_review(
    draft_ref: str,
    document_id: str,
    decision: str,
    reason: str,
) -> dict:
    """Record an authorized officer's explicit age-proof document review."""
    return PortalClient().request(
        "POST",
        f"/api/agent/officer/age-proof-review-drafts/{draft_ref}/decision",
        _access_token(),
        json_body={
            "document_id": document_id,
            "decision": decision,
            "reason": reason,
        },
    )
