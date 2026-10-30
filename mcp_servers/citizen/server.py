import base64
import functools
import logging
import math
import re
import secrets
import threading
import time
from datetime import datetime
from typing import Annotated, Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import BlobResourceContents, CallToolResult, EmbeddedResource, TextContent
from pydantic import Field

from mcp_servers.citizen.tools import (
    attach_age_proof as attach_citizen_age_proof,
    create_or_resume_application_draft as create_citizen_draft,
    get_application_status as check_application_status,
    get_application_slip as download_application_slip,
    get_scheme_information,
    request_citizen_otp as send_citizen_otp,
    read_application_draft as read_citizen_draft,
    save_application_draft as save_citizen_draft,
    submit_pension_application as create_pension_application,
    validate_age_proof as validate_citizen_age_proof,
    verify_citizen_otp as verify_citizen_session_otp,
    withdraw_application as withdraw_citizen_application,
)
from mcp_servers.common.config import CITIZEN_MCP_PORT
from mcp_servers.common.errors import AuthorizationError, PortalIntegrationError

CitizenSessionHandle = Annotated[
    str,
    Field(description=(
        "Opaque reference returned by verify_citizen_otp. Pass it only to "
        "protected citizen tools; never display or repeat it. The actual "
        "portal session token is held privately by the MCP server."
    )),
]

mcp = FastMCP(
    "Sewa Setu Citizen",
    instructions=(
        "Help a citizen check or prepare their own Old Age Pension application. "
        "Citizen MCP transport does not require OAuth. OTP verification is "
        "mandatory before any personal details are requested or application "
        "tools are used. Keep the entire interaction in this chat; never open "
        "a website, browser, or webview. Use compact, labeled form-style "
        "prompts with one input per message. Start with: "
        "'**Mobile number** (10 digits): Please enter your mobile number.' "
        "After they provide it, call request_citizen_otp. If successful, "
        "immediately reply: '**OTP verification**\nOTP sent successfully. "
        "Please enter the 6-digit OTP you received.' End the turn and wait "
        "for the next chat message. Do not call verify_citizen_otp in the "
        "same turn as the OTP request. On the next turn, call "
        "verify_citizen_otp with the same mobile number and the supplied "
        "code. Never claim verification without a successful tool result. "
        "On invalid or expired OTP, ask for another code and wait. Never "
        "repeat or reveal the OTP. Verification automatically creates or "
        "resumes the citizen's draft using the existing protected portal "
        "endpoint; do not make a separate preflight call unless the tool "
        "explicitly reports that draft creation needs retrying. It returns "
        "an opaque session_ref, not the portal credential. Pass session_ref "
        "only to protected citizen tools and never display it. If the "
        "session_ref is expired or unavailable, ask the citizen to request "
        "a new OTP and verify again. If draft creation fails after OTP "
        "verification, explain that the session is verified and retry "
        "create_or_resume_application_draft using the returned session_ref. "
        "After every tool call, send a user-facing follow-up with the result "
        "and next step; never end a turn silently after a tool call. "
        "On tool/network errors, explain the failure and do not claim success. "
        "If preflight reports "
        "application_exists, tell the citizen the returned reference and "
        "status without calling check_my_application again unless they ask for "
        "more details. Otherwise present the application as a guided form. "
        "Ask exactly one missing field per message, using a clear label and "
        "format hint; wait for its answer before asking the next. Use this "
        "order: applicant name; date of birth (DD/MM/YYYY); gender; marital "
        "status; husband's name only when applicable; village; block; bank "
        "account number; IFSC; age-proof document attachment. Mark each "
        "prompt as '**Application details - [field]**', then ask one short "
        "question with its expected format (for example DD/MM/YYYY for date "
        "of birth, digits only for bank account, and 11 characters for IFSC). "
        "After each answer, save only that field immediately "
        "with save_application_draft before asking the next, so progress "
        "persists. Ask only for fields missing from the existing draft and "
        "never ask again for saved answers. Bank account numbers must be "
        "collected as strings, preserving leading zeros; never repeat the "
        "complete number in a prompt, response, or preview. Ask early for the "
        "actual age-proof attachment. If the citizen will attach it later, "
        "retain the draft and give its reference. Never ask the citizen to "
        "paste base64 or document contents into chat. "
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
        "using get_my_application_slip. Return the actual application number "
        "and status from the submission result; never invent either. "
        "A citizen can withdraw their own pending application only after explicit "
        "confirmation. Use official portal rules and do not promise eligibility. "
    ),
    host="0.0.0.0",
    port=CITIZEN_MCP_PORT,
    streamable_http_path="/mcp",
    max_request_body_size=8 * 1024 * 1024,
)

logger = logging.getLogger(__name__)

_citizen_sessions: dict[str, tuple[str, float]] = {}
_citizen_sessions_lock = threading.Lock()


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


def _store_citizen_session(result: dict) -> str:
    token = result.get("session_token")
    if not isinstance(token, str) or not token:
        raise PortalIntegrationError(
            "OTP verification returned no citizen session token."
        )
    try:
        expires_in = min(
            float(result["expires_in"]),
            float(result["session_expires_in"]),
        )
    except (KeyError, TypeError, ValueError):
        raise PortalIntegrationError(
            "OTP verification returned an invalid session lifetime."
        ) from None
    if not math.isfinite(expires_in) or expires_in <= 0:
        raise PortalIntegrationError(
            "OTP verification returned an expired citizen session."
        )

    now = time.monotonic()
    session_ref = secrets.token_urlsafe(32)
    with _citizen_sessions_lock:
        expired = [
            reference for reference, (_, deadline) in _citizen_sessions.items()
            if deadline <= now
        ]
        for reference in expired:
            _citizen_sessions.pop(reference, None)
        _citizen_sessions[session_ref] = (token, now + expires_in)
    return session_ref


def _discard_citizen_session(session_ref: str) -> None:
    with _citizen_sessions_lock:
        _citizen_sessions.pop(session_ref, None)


def _citizen_session_token(session_ref: str) -> str:
    if not isinstance(session_ref, str) or not session_ref:
        raise AuthorizationError(
            "Verify your mobile number before using this citizen tool."
        )
    now = time.monotonic()
    with _citizen_sessions_lock:
        session = _citizen_sessions.get(session_ref)
        if session and session[1] <= now:
            _citizen_sessions.pop(session_ref, None)
            session = None
    if not session:
        raise AuthorizationError(
            "Your verified citizen session expired or is unavailable. "
            "Request a new OTP and verify it again."
        )
    return session[0]


def _citizen_call(
    session_ref: str, operation: Callable[[str], Any]
) -> Any:
    token = _citizen_session_token(session_ref)
    try:
        return operation(token)
    except PortalIntegrationError as exc:
        if "failed (401)" in str(exc):
            _discard_citizen_session(session_ref)
            raise AuthorizationError(
                "Your verified citizen session expired. Request a new OTP "
                "and verify it again."
            ) from exc
        raise


def _masked_bank_account(value) -> str:
    if not isinstance(value, str) or not value:
        return "Not provided"
    if "*" in value:
        return value
    digits = re.sub(r"\D", "", value)
    if not digits:
        return "****"
    return "*" * max(4, len(digits) - 4) + digits[-4:]


def _sanitize_draft_response(result: dict) -> dict:
    safe_result = dict(result)
    if isinstance(safe_result.get("details"), dict):
        details = dict(safe_result["details"])
        if "bank_account" in details:
            details["bank_account"] = _masked_bank_account(
                details["bank_account"]
            )
        safe_result["details"] = details
    return safe_result


def _draft_preview(draft: dict) -> str:
    details = draft.get("details") or {}
    labels = (
        ("Applicant name", "applicant_name"),
        ("Date of birth", "dob"),
        ("Gender", "gender"),
        ("Marital status", "marital_status"),
        ("Husband's name", "husband_name"),
        ("Village", "village"),
        ("Block", "block"),
        ("Bank account", "bank_account"),
        ("IFSC", "ifsc"),
    )
    lines = ["**Old Age Pension application preview**"]
    for label, key in labels:
        value = details.get(key)
        if key == "bank_account":
            value = _masked_bank_account(value)
        elif not value:
            value = "Not provided"
        lines.append("- %s: %s" % (label, value))
    lines.extend((
        "- Age-proof status: %s" % draft.get("document_status", "Unknown"),
        "",
        "Please confirm that these details are correct and that you agree "
        "with the applicant declaration before submission.",
    ))
    return "\n".join(lines)


@mcp.tool()
@_timed_tool
def pension_scheme_information() -> dict:
    """Get current pension rules and blocks enabled for agent applications."""
    result = dict(get_scheme_information())
    result.pop("application_form_url", None)
    return {
        **result,
        "assistant_message": (
            "Share the returned official scheme requirements and eligibility "
            "information in this chat. Do not open or direct the citizen to a website."
        ),
    }


@mcp.tool()
@_timed_tool
def request_citizen_otp(mobile: str) -> dict:
    """Request an OTP; on success present the labeled OTP entry prompt."""
    mobile = mobile.strip() if isinstance(mobile, str) else ""
    if not re.fullmatch(r"\d{10}", mobile):
        return {
            "status": "invalid_input",
            "assistant_message": (
                "**Mobile number** (10 digits): Please enter a valid "
                "10-digit mobile number."
            ),
            "next_action": "wait_for_mobile_number",
        }
    result = send_citizen_otp(mobile)
    if result.get("status") != "otp_sent":
        return result
    return {
        **result,
        "assistant_message": (
            "**OTP verification**\n"
            "OTP sent successfully. Please enter the 6-digit OTP you received."
        ),
        "next_action": "wait_for_citizen_otp",
    }


@mcp.tool()
@_timed_tool
def verify_citizen_otp(mobile: str, otp: str) -> dict:
    """Verify OTP and automatically create or resume the protected draft."""
    mobile = mobile.strip() if isinstance(mobile, str) else ""
    otp = otp.strip() if isinstance(otp, str) else ""
    if not re.fullmatch(r"\d{10}", mobile) or not re.fullmatch(r"\d{6}", otp):
        return {
            "verified": False,
            "assistant_message": (
                "**OTP verification**\n"
                "Enter the 6-digit OTP sent to your 10-digit mobile number."
            ),
            "next_action": "wait_for_valid_otp",
        }
    try:
        result = verify_citizen_session_otp(mobile, otp)
    except PortalIntegrationError as exc:
        if "invalid or expired otp" in str(exc).lower():
            return {
                "verified": False,
                "assistant_message": (
                    "That OTP is invalid or expired. Please try again."
                ),
                "next_action": "wait_for_citizen_otp",
            }
        raise
    session_ref = _store_citizen_session(result)
    session_token = result["session_token"]
    try:
        draft = create_citizen_draft(session_token)
    except PortalIntegrationError:
        return {
            "verified": True,
            "session_ref": session_ref,
            "draft_ready": False,
            "assistant_message": (
                "Your mobile number is verified, but your application draft "
                "could not be opened yet. I can retry that step without asking "
                "you to verify again."
            ),
            "next_action": "retry_create_or_resume_application_draft",
        }
    if draft.get("application_exists"):
        _discard_citizen_session(session_ref)
        return {
            "verified": True,
            "application_exists": True,
            "application_number": draft.get("application_number"),
            "status": draft.get("status"),
            "assistant_message": (
                "Your mobile number is verified. An application already exists; "
                "no new draft was created. Its application number and status are "
                "shown in the tool result."
            ),
            "next_action": "report_existing_application",
        }
    if not draft.get("draft_ref"):
        _discard_citizen_session(session_ref)
        raise PortalIntegrationError(
            "The portal did not return an application draft reference."
        )
    draft = _sanitize_draft_response(draft)
    return {
        "verified": True,
        "session_ref": session_ref,
        "draft": draft,
        "assistant_message": (
            "Your mobile number is verified and your draft is ready. "
            "Continue with the first missing application detail."
        ),
        "next_action": "collect_application_details",
    }


@mcp.tool()
@_timed_tool
def check_my_application(session_ref: CitizenSessionHandle) -> dict:
    """Check the authenticated citizen's application or saved draft."""
    result = _citizen_call(
        session_ref,
        lambda token: check_application_status(token),
    )
    return {
        **result,
        "assistant_message": (
            "Report the returned application or draft status and its actual "
            "reference. Do not invent a status or application number."
        ),
    }


@mcp.tool()
@_timed_tool
def create_or_resume_application_draft(
    session_ref: CitizenSessionHandle,
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
    result = _citizen_call(
        session_ref,
        lambda token: create_citizen_draft(
            token,
            **{key: value for key, value in details.items() if value},
        ),
    )
    if result.get("application_exists"):
        return {
            **result,
            "assistant_message": (
                "An application already exists. Report its actual number and "
                "status; do not start a new draft."
            ),
        }
    if not result.get("draft_ref"):
        raise PortalIntegrationError(
            "The portal did not return an application draft reference."
        )
    result = _sanitize_draft_response(result)
    return {
        **result,
        "assistant_message": (
            "The application draft is ready. Ask for the first missing "
            "application detail in the guided sequence."
        ),
    }


@mcp.tool()
@_timed_tool
def save_application_draft(
    draft_ref: str,
    session_ref: CitizenSessionHandle,
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
    """Save one or more answers to the authenticated citizen's draft."""
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
    updates = {key: value for key, value in details.items() if value}
    if not updates:
        return {
            "saved": False,
            "assistant_message": (
                "No application detail was provided. Please answer the "
                "current labeled field prompt."
            ),
            "next_action": "wait_for_application_detail",
        }
    if "dob" in updates:
        try:
            datetime.strptime(updates["dob"], "%d/%m/%Y")
        except ValueError:
            return {
                "saved": False,
                "assistant_message": (
                    "Please enter your date of birth as DD/MM/YYYY, for example "
                    "15/04/1960."
                ),
                "next_action": "retry_date_of_birth",
            }
    if "ifsc" in updates and updates["ifsc"]:
        ifsc = re.sub(r"[\s-]", "", updates["ifsc"]).upper()
        if not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
            return {
                "saved": False,
                "assistant_message": (
                    "That IFSC format is invalid. Please enter the 11-character "
                    "bank IFSC, for example SBIN0003077."
                ),
                "next_action": "retry_ifsc",
            }
        updates["ifsc"] = ifsc
    result = _citizen_call(
        session_ref,
        lambda token: save_citizen_draft(
            draft_ref,
            token,
            **updates,
        ),
    )
    result = _sanitize_draft_response(result)
    return {
        **result,
        "assistant_message": (
            "That application detail was saved securely. Continue by asking "
            "for the next missing field; do not repeat or echo sensitive values."
        ),
        "next_action": "collect_next_application_detail",
    }


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
    session_ref: CitizenSessionHandle,
) -> dict:
    """Attach an actual age-proof file to this authenticated citizen's saved draft."""
    result = _citizen_call(
        session_ref,
        lambda token: attach_citizen_age_proof(
            draft_ref, filename, file_base64, token
        ),
    )
    return {
        **result,
        "assistant_message": (
            "The age-proof file was attached. Run validate_age_proof next; "
            "do not claim it is approved until the returned status says VALID."
        ),
        "next_action": "validate_age_proof",
    }


@mcp.tool()
@_timed_tool
def validate_age_proof(
    draft_ref: str, session_ref: CitizenSessionHandle
) -> dict:
    """Check stored file readability and report officer-review status."""
    result = _citizen_call(
        session_ref,
        lambda token: validate_citizen_age_proof(draft_ref, token),
    )
    status = result.get("document_status")
    if status == "VALID":
        message = (
            "The age-proof status is VALID. Get the saved draft preview next, "
            "then ask for explicit confirmation before submission."
        )
        next_action = "review_and_confirm"
    elif status == "REVIEW_REQUIRED":
        message = (
            "The file checks passed, but an authorized officer must review the "
            "document. Do not submit while it is awaiting review."
        )
        next_action = "wait_for_officer_review"
    else:
        message = (
            "The age-proof document is not ready for submission. Follow the "
            "returned status and message to correct or complete it."
        )
        next_action = "resolve_document_status"
    return {
        **result,
        "assistant_message": message,
        "next_action": next_action,
    }


@mcp.tool()
@_timed_tool
def get_application_draft(
    draft_ref: str, session_ref: CitizenSessionHandle
) -> dict:
    """Read the citizen's saved draft and masked summary before confirmation."""
    result = _citizen_call(
        session_ref,
        lambda token: read_citizen_draft(draft_ref, token),
    )
    details = dict(result.get("details") or {})
    details["bank_account"] = _masked_bank_account(details.get("bank_account"))
    safe_result = {**result, "details": details}
    preview = _draft_preview(safe_result)
    if result.get("document_status") != "VALID":
        preview += (
            "\n\nThis draft is not ready to submit because its age-proof "
            "status is not VALID. Do not submit it."
        )
        return {
            **safe_result,
            "assistant_message": preview,
            "next_action": "resolve_document_status",
        }
    return {
        **safe_result,
        "assistant_message": preview,
        "next_action": "await_confirmation",
    }


@mcp.tool()
@_timed_tool
def get_my_application_slip(session_ref: CitizenSessionHandle) -> CallToolResult:
    """Get the latest application acknowledgment as a downloadable PDF attachment."""
    pdf_bytes, disposition = _citizen_call(
        session_ref,
        lambda token: download_application_slip(token),
    )
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
def withdraw_my_application(
    application_id: int, confirm: bool, session_ref: CitizenSessionHandle
) -> dict:
    """Withdraw your own pending application after you explicitly confirm.

    Withdrawal is final for this application. Only use confirm=True after the
    citizen explicitly asks to withdraw the specified application.
    """
    if confirm is not True:
        return {
            "withdrawn": False,
            "assistant_message": (
                "The application was not withdrawn. Ask for explicit "
                "confirmation before retrying."
            ),
            "next_action": "await_explicit_confirmation",
        }
    result = _citizen_call(
        session_ref,
        lambda token: withdraw_citizen_application(
            application_id=application_id,
            confirm=confirm,
            session_token=token,
        ),
    )
    return {
        **result,
        "assistant_message": (
            "Report the returned withdrawal status and application number. "
            "Do not claim withdrawal unless the portal confirms it."
        ),
    }


@mcp.tool()
@_timed_tool
def submit_pension_application(
    draft_ref: str,
    declaration_confirmed: bool,
    summary_confirmed: bool,
    session_ref: CitizenSessionHandle,
) -> dict:
    """Submit a validated draft only after the citizen confirms its masked summary."""
    if declaration_confirmed is not True or summary_confirmed is not True:
        return {
            "submitted": False,
            "assistant_message": (
                "I need your explicit confirmation of both the application "
                "summary and declaration before submitting."
            ),
            "next_action": "await_explicit_confirmation",
        }
    result = _citizen_call(
        session_ref,
        lambda token: create_pension_application(
            draft_ref=draft_ref,
            session_token=token,
            declaration_confirmed=True,
            summary_confirmed=True,
        ),
    )
    application_number = result.get("application_number")
    status = result.get("status")
    if not application_number or not status:
        raise PortalIntegrationError(
            "The portal submission response did not include an application "
            "number and status."
        )
    if str(result.get("message", "")).lower().startswith(
        "an application already exists"
    ):
        return {
            **result,
            "submitted": False,
            "application_exists": True,
            "assistant_message": (
                "No new application was submitted because an application "
                "already exists. Application number: %s. Status: %s."
                % (application_number, status)
            ),
            "next_action": "report_existing_application",
        }
    return {
        **result,
        "submitted": True,
        "assistant_message": (
            "Your application was submitted. Application number: %s. "
            "Status: %s." % (application_number, status)
        ),
        "next_action": "offer_application_acknowledgment",
    }


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
