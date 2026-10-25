from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from mcp_servers.common.auth import PortalTokenVerifier
from mcp_servers.common.config import (
    OFFICER_MCP_PORT,
    OFFICER_RESOURCE_URL,
    PORTAL_ISSUER_URL,
)
from mcp_servers.officer.tools import (
    add_field_verification_note as record_field_note,
    batch_decide_applications as record_batch_decision,
    check_duplicate_claims as find_duplicate_claims,
    decide_application as record_decision,
    get_applicant_history as read_applicant_history,
    get_application_document as read_application_document,
    get_age_proof_review_document as read_age_proof_review_document,
    decide_age_proof_review as record_age_proof_review,
    list_age_proof_review_drafts as read_age_proof_review_drafts,
    get_application as read_application,
    get_queue_summary as read_queue_summary,
    get_village_breakdown as read_village_breakdown,
    list_deemed_approved_applications as read_deemed_approved_applications,
    get_approved_applications as read_filtered_approved_applications,
    list_approved_applications as read_approved_applications,
    list_pending_applications as read_pending_applications,
    list_sla_risk_applications as read_sla_risk_applications,
    reassign_or_flag_case as flag_case,
    search_applications as find_applications,
    verify_bank_details as check_bank_details,
)

mcp = FastMCP(
    "Sewa Setu Block Officer",
    instructions=(
        "Support the authenticated block officer by reviewing the configured "
        "agent-enabled block queue, including read-only review of approved "
        "applications and their recorded decision history, and recording an "
        "approve/reject decision with "
        "a reason. Never decide a case without the officer's explicit instruction. "
        "Approved application retrieval is limited by backend authorization to "
        "agent-enabled blocks and never changes application records. "
        "Age-proof files that passed basic file checks but could not be authenticated "
        "are held as REVIEW_REQUIRED drafts. Inspect the actual document and compare "
        "readable name and age evidence with the saved draft before recording VALID or "
        "INVALID. Do not treat OCR or file-signature checks as proof of authenticity. "
        "Record a reason for every document-review outcome."
    ),
    host="0.0.0.0",
    port=OFFICER_MCP_PORT,
    streamable_http_path="/mcp",
    auth=AuthSettings(
        issuer_url=PORTAL_ISSUER_URL,
        resource_server_url=OFFICER_RESOURCE_URL,
        validate_token_resource=False,
        required_scopes=["officer"],
    ),
    token_verifier=PortalTokenVerifier(OFFICER_RESOURCE_URL, "officer"),
)


@mcp.tool()
def list_pending_applications(limit: int = 20) -> dict:
    """List pending applications in the agent-enabled block queue."""
    return read_pending_applications(limit=limit)


@mcp.tool()
def list_approved_applications(limit: int = 20) -> dict:
    """List approved applications in the agent-enabled block queue."""
    return read_approved_applications(limit=limit)


@mcp.tool()
def get_approved_applications(
    limit: int = 20,
    offset: int = 0,
    submitted_from: str = "",
    submitted_to: str = "",
    decision_from: str = "",
    decision_to: str = "",
    block: str = "",
) -> dict:
    """Retrieve approved applications and decision history within enabled blocks."""
    return read_filtered_approved_applications(
        limit=limit,
        offset=offset,
        submitted_from=submitted_from,
        submitted_to=submitted_to,
        decision_from=decision_from,
        decision_to=decision_to,
        block=block,
    )


@mcp.tool()
def review_application(application_id: int) -> dict:
    """Read an application and its audit trail before making a decision."""
    return read_application(application_id=application_id)


@mcp.tool()
def decide_on_application(
    application_id: int,
    decision: str,
    reason: str,
) -> dict:
    """Approve or reject a pending application with a recorded reason."""
    return record_decision(
        application_id=application_id,
        decision=decision,
        reason=reason,
    )


@mcp.tool()
def list_sla_risk_applications(days_threshold: int = 3, limit: int = 50) -> dict:
    """List pending cases at risk of deemed approval within this many days."""
    return read_sla_risk_applications(
        days_threshold=days_threshold,
        limit=limit,
    )


@mcp.tool()
def list_deemed_approved_applications(days_back: int = 30, limit: int = 50) -> dict:
    """List cases deemed approved during the requested lookback period."""
    return read_deemed_approved_applications(days_back=days_back, limit=limit)


@mcp.tool()
def search_applications(
    query: str,
    status: str = "",
    limit: int = 20,
) -> dict:
    """Search applications by name, mobile, application number, or bank account."""
    return find_applications(query=query, status=status, limit=limit)


@mcp.tool()
def get_applicant_history(mobile: str) -> dict:
    """Read an applicant's history and audit interactions by mobile number."""
    return read_applicant_history(mobile=mobile)


@mcp.tool()
def check_duplicate_claims(application_id: int) -> dict:
    """Find cases matching this application's bank, household, or mobile details."""
    return find_duplicate_claims(application_id=application_id)


@mcp.tool()
def verify_bank_details(application_id: int) -> dict:
    """Check bank field formats and identify known legacy IFSC prefixes."""
    return check_bank_details(application_id=application_id)


@mcp.tool()
def get_application_document(application_id: int) -> dict:
    """Read age-proof document metadata and base64 contents for inspection."""
    return read_application_document(application_id=application_id)


@mcp.tool()
def add_field_verification_note(
    application_id: int,
    note: str,
    verified_by: str,
) -> dict:
    """Add a preliminary field-verification audit note without a final decision."""
    return record_field_note(
        application_id=application_id,
        note=note,
        verified_by=verified_by,
    )


@mcp.tool()
def batch_decide_applications(
    application_ids: list[int],
    decision: str,
    reason: str,
) -> dict:
    """Approve or reject up to 100 pending applications as one atomic operation."""
    return record_batch_decision(
        application_ids=application_ids,
        decision=decision,
        reason=reason,
    )


@mcp.tool()
def reassign_or_flag_case(
    application_id: int,
    flag_type: str,
    reason: str,
) -> dict:
    """Flag a pending case for follow-up and exclude it from deemed approval."""
    return flag_case(
        application_id=application_id,
        flag_type=flag_type,
        reason=reason,
    )


@mcp.tool()
def get_queue_summary(block: str = "") -> dict:
    """Get a queue snapshot and this month's officer decision counts."""
    return read_queue_summary(block=block)


@mcp.tool()
def get_village_breakdown(block: str = "") -> dict:
    """Group pending and approved application counts by village."""
    return read_village_breakdown(block=block)


@mcp.tool()
def list_age_proof_review_drafts() -> dict:
    """List saved citizen drafts awaiting age-proof review in enabled blocks."""
    return read_age_proof_review_drafts()


@mcp.tool()
def get_age_proof_review_document(draft_ref: str) -> dict:
    """Read a pending draft's document and masked details for manual inspection."""
    return read_age_proof_review_document(draft_ref=draft_ref)


@mcp.tool()
def decide_age_proof_review(
    draft_ref: str,
    document_id: str,
    decision: str,
    reason: str,
) -> dict:
    """Record VALID, INVALID, or REVIEW_REQUIRED after actual officer review."""
    return record_age_proof_review(
        draft_ref=draft_ref,
        document_id=document_id,
        decision=decision,
        reason=reason,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
