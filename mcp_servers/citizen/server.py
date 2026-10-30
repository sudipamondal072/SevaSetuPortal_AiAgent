import base64
import functools
import logging
import time
from typing import Annotated

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from pydantic import Field

from mcp_servers.citizen.tools import (
    attach_age_proof as attach_citizen_age_proof,
    create_or_resume_application_draft as create_citizen_draft,
    get_application_status as check_application_status,
    get_application_slip as download_application_slip,
    get_scheme_information,
    read_application_draft as read_citizen_draft,
    save_application_draft as save_citizen_draft,
    submit_pension_application as create_pension_application,
    validate_age_proof as validate_citizen_age_proof,
    withdraw_application as withdraw_citizen_application,
)
from mcp_servers.common.auth import PortalTokenVerifier
from mcp_servers.common.config import (
    CITIZEN_MCP_PORT,
    CITIZEN_RESOURCE_URL,
    PORTAL_ISSUER_URL,
)

mcp = FastMCP(
    "Sewa Setu Citizen",
    instructions=(
        "Help a citizen check or prepare their own Old Age Pension application. "
            "When they ask to apply, call create_or_resume_application_draft once as the "
            "preflight; the backend checks for a blocking application and creates or resumes "
            "the authenticated citizen's draft in the same request. If it reports "
            "application_exists, tell the citizen the returned reference and status without "
            "calling check_my_application again unless they ask for more details. Otherwise "
            "ask for all missing application details together and save the collected fields "
            "in one save_application_draft call, not one call per field. Ask early for the "
            "actual age-proof attachment. If the citizen will attach it later, retain the "
            "draft and give its reference; resume it without recollecting saved answers. "
            "Never ask the citizen to paste base64 or document contents into chat. "
        "A visible chat image is not proof that this MCP server received or stored "
        "the file. Call attach_age_proof only when the client supplies the actual "
        "file bytes to that tool. If it cannot, say the upload did not happen; the "
        "chat frontend must upload the file as multipart field 'document' to the "
        "authenticated portal draft age-proof endpoint before reporting receipt. "
        "Unknown village-to-block mapping is not a reason to reject; record the "
        "citizen's village and block as stated, without claiming an unverified mapping. "
        "Collect bank account numbers as strings, preserve leading zeros, remove "
        "spaces, and never repeat the complete number; only masked values may be shown. "
        "After attaching a document, call validate_age_proof. Basic file checks are "
        "not authenticity verification. A REVIEW_REQUIRED document must be reviewed "
        "by an authorized officer; do not submit unless backend status is VALID. "
        "Do not call pension_scheme_information just to pre-check an application; "
        "the portal enforces eligibility and application rules directly on submission. "
        "Before submission, call get_application_draft once, present its masked summary, "
        "and obtain the citizen's explicit confirmation of both the details and "
        "declaration. Submit only after officer document review is VALID. Never repeat "
        "a tool request while it is still processing, and never claim submission "
        "succeeded unless the portal returns the application number and actual status. "
        "After a successful submission, offer to show or download the acknowledgment "
        "using get_my_application_slip. "
        "A citizen can withdraw their own pending application only after explicit "
        "confirmation. Use official portal rules and do not promise eligibility. "
    ),
    host="0.0.0.0",
    port=CITIZEN_MCP_PORT,
    streamable_http_path="/mcp",
    auth=AuthSettings(
        issuer_url=PORTAL_ISSUER_URL,
        resource_server_url=CITIZEN_RESOURCE_URL,
        validate_token_resource=False,
        required_scopes=["citizen"],
    ),
    token_verifier=PortalTokenVerifier(CITIZEN_RESOURCE_URL, "citizen"),
    max_request_body_size=8 * 1024 * 1024,
)

logger = logging.getLogger(__name__)


def _timed_tool(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            logger.info(
                "mcp_tool name=%s elapsed_ms=%.2f",
                function.__name__,
                (time.perf_counter() - started) * 1000,
            )

    return wrapped


@mcp.tool()
@_timed_tool
def pension_scheme_information() -> dict:
    """Get current pension rules and blocks enabled for agent applications."""
    return get_scheme_information()


@mcp.tool()
@_timed_tool
def check_my_application() -> dict:
    """Check the authenticated citizen's application or saved draft."""
    return check_application_status()


@mcp.tool()
@_timed_tool
def create_or_resume_application_draft(
    applicant_name: str = "",
    date_of_birth: str = "",
    village: str = "",
    block: str = "",
    bank_account: str = "",
    ifsc: str = "",
    gender: str = "",
    marital_status: str = "",
    husband_name: str = "",
) -> dict:
    """Preflight and create or resume the authenticated citizen's saved draft.

    This request checks for an existing blocking application, so do not call
    check_my_application first. Partial details are allowed in a draft.
    """
    details = {
        "applicant_name": applicant_name,
        "dob": date_of_birth,
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
        "gender": gender,
        "marital_status": marital_status,
        "husband_name": husband_name,
    }
    return create_citizen_draft(**{
        key: value for key, value in details.items() if value
    })


@mcp.tool()
@_timed_tool
def save_application_draft(
    draft_ref: str,
    applicant_name: str = "",
    date_of_birth: str = "",
    village: str = "",
    block: str = "",
    bank_account: str = "",
    ifsc: str = "",
    gender: str = "",
    marital_status: str = "",
    husband_name: str = "",
) -> dict:
    """Save collected fields and resume the same draft in a later chat."""
    details = {
        "applicant_name": applicant_name,
        "dob": date_of_birth,
        "village": village,
        "block": block,
        "bank_account": bank_account,
        "ifsc": ifsc,
        "gender": gender,
        "marital_status": marital_status,
        "husband_name": husband_name,
    }
    return save_citizen_draft(
        draft_ref,
        **{key: value for key, value in details.items() if value}
    )


@mcp.tool()
@_timed_tool
def attach_age_proof(
    draft_ref: str,
    filename: str,
    file_base64: Annotated[
        str,
        Field(description=(
            "Base64-encoded actual file bytes supplied by the chat client for an "
            "attached PDF, JPG, JPEG, or PNG. Never infer bytes from image vision "
            "or ask the citizen to type or paste base64."
        )),
    ],
) -> dict:
    """Attach an actual age-proof file to this authenticated citizen's saved draft."""
    return attach_citizen_age_proof(draft_ref, filename, file_base64)


@mcp.tool()
@_timed_tool
def validate_age_proof(draft_ref: str) -> dict:
    """Check stored file readability and report officer-review status."""
    return validate_citizen_age_proof(draft_ref)


@mcp.tool()
@_timed_tool
def get_application_draft(draft_ref: str) -> dict:
    """Read the citizen's saved draft and masked summary before confirmation."""
    return read_citizen_draft(draft_ref)


@mcp.tool()
@_timed_tool
def get_my_application_slip() -> CallToolResult:
    """Get the latest application acknowledgment as a downloadable PDF attachment."""
    pdf_bytes, disposition = download_application_slip()
    filename = "application-slip.pdf"
    marker = 'filename="'
    if marker in disposition:
        filename = disposition.split(marker, 1)[1].split('"', 1)[0]
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text="Your application acknowledgment is attached as %s." % filename,
            ),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri="sewasetu://citizen/application-slip",
                    mimeType="application/pdf",
                    blob=base64.b64encode(pdf_bytes).decode("ascii"),
                ),
            ),
        ]
    )


@mcp.tool()
@_timed_tool
def withdraw_my_application(application_id: int, confirm: bool) -> dict:
    """Withdraw your own pending application after you explicitly confirm.

    Withdrawal is final for this application. Only use confirm=True after the
    citizen explicitly asks to withdraw the specified application.
    """
    return withdraw_citizen_application(
        application_id=application_id,
        confirm=confirm,
    )


@mcp.tool()
@_timed_tool
def submit_pension_application(
    draft_ref: str,
    declaration_confirmed: bool,
    summary_confirmed: bool,
) -> dict:
    """Submit a validated draft only after the citizen confirms its masked summary."""
    return create_pension_application(
        draft_ref=draft_ref,
        declaration_confirmed=declaration_confirmed,
        summary_confirmed=summary_confirmed,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
