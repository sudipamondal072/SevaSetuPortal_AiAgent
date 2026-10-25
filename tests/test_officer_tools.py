import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mcp_servers.common.errors import AuthorizationError
from mcp_servers.officer import tools
from mcp_servers.officer.server import mcp


def test_all_requested_tools_are_registered_on_officer_mcp_server():
    registered_names = {tool.name for tool in mcp._tool_manager.list_tools()}
    requested_names = {
        "list_sla_risk_applications",
        "list_deemed_approved_applications",
        "list_approved_applications",
        "get_approved_applications",
        "search_applications",
        "get_applicant_history",
        "check_duplicate_claims",
        "verify_bank_details",
        "get_application_document",
        "add_field_verification_note",
        "batch_decide_applications",
        "reassign_or_flag_case",
        "get_queue_summary",
        "get_village_breakdown",
        "list_age_proof_review_drafts",
        "get_age_proof_review_document",
        "decide_age_proof_review",
    }

    assert requested_names <= registered_names


@pytest.mark.parametrize(
    ("tool_name", "args", "method", "path", "params", "json_body"),
    [
        (
            "list_sla_risk_applications",
            {"days_threshold": 4, "limit": 10},
            "GET",
            "/api/agent/officer/sla-risk",
            {"days_threshold": 4, "limit": 10},
            None,
        ),
        (
            "list_deemed_approved_applications",
            {"days_back": 14, "limit": 12},
            "GET",
            "/api/agent/officer/deemed-approved",
            {"days_back": 14, "limit": 12},
            None,
        ),
        (
            "list_approved_applications",
            {"limit": 12},
            "GET",
            "/api/agent/officer/approved-applications",
            {"limit": 12},
            None,
        ),
        (
            "get_approved_applications",
            {
                "limit": 5,
                "offset": 0,
                "submitted_from": "2026-10-01",
                "submitted_to": "2026-10-24",
                "decision_from": "",
                "decision_to": "",
                "block": "Sonari",
            },
            "GET",
            "/api/agent/officer/approved-applications",
            {
                "limit": 5,
                "offset": 0,
                "submitted_from": "2026-10-01",
                "submitted_to": "2026-10-24",
                "decision_from": "",
                "decision_to": "",
                "block": "Sonari",
            },
            None,
        ),
        (
            "search_applications",
            {"query": "9000000001", "status": "PENDING", "limit": 7},
            "GET",
            "/api/agent/officer/search",
            {"query": "9000000001", "status": "PENDING", "limit": 7},
            None,
        ),
        (
            "get_applicant_history",
            {"mobile": "9000000001"},
            "GET",
            "/api/agent/officer/applicant-history",
            {"mobile": "9000000001"},
            None,
        ),
        (
            "check_duplicate_claims",
            {"application_id": 11},
            "GET",
            "/api/agent/officer/applications/11/duplicates",
            None,
            None,
        ),
        (
            "verify_bank_details",
            {"application_id": 12},
            "GET",
            "/api/agent/officer/applications/12/bank-verification",
            None,
            None,
        ),
        (
            "get_application_document",
            {"application_id": 13},
            "GET",
            "/api/agent/officer/applications/13/document",
            None,
            None,
        ),
        (
            "add_field_verification_note",
            {"application_id": 14, "note": "Visited", "verified_by": "Inspector"},
            "POST",
            "/api/agent/officer/applications/14/field-note",
            None,
            {"note": "Visited", "verified_by": "Inspector"},
        ),
        (
            "batch_decide_applications",
            {"application_ids": [15, 16], "decision": "approve", "reason": "Verified"},
            "POST",
            "/api/agent/officer/applications/batch-decision",
            None,
            {"application_ids": [15, 16], "decision": "approve", "reason": "Verified"},
        ),
        (
            "reassign_or_flag_case",
            {
                "application_id": 17,
                "flag_type": "ADDRESS_UNVERIFIED",
                "reason": "Verify address",
            },
            "POST",
            "/api/agent/officer/applications/17/flag",
            None,
            {"flag_type": "ADDRESS_UNVERIFIED", "reason": "Verify address"},
        ),
        (
            "get_queue_summary",
            {"block": "Sonari"},
            "GET",
            "/api/agent/officer/queue-summary",
            {"block": "Sonari"},
            None,
        ),
        (
            "get_village_breakdown",
            {"block": "Sonari"},
            "GET",
            "/api/agent/officer/village-breakdown",
            {"block": "Sonari"},
            None,
        ),
        (
            "list_age_proof_review_drafts",
            {},
            "GET",
            "/api/agent/officer/age-proof-review-drafts",
            None,
            None,
        ),
        (
            "get_age_proof_review_document",
            {"draft_ref": "draft-ref"},
            "GET",
            "/api/agent/officer/age-proof-review-drafts/draft-ref/document",
            None,
            None,
        ),
        (
            "decide_age_proof_review",
            {
                "draft_ref": "draft-ref",
                "document_id": "document-id",
                "decision": "VALID",
                "reason": "Verified",
            },
            "POST",
            "/api/agent/officer/age-proof-review-drafts/draft-ref/decision",
            None,
            {
                "document_id": "document-id",
                "decision": "VALID",
                "reason": "Verified",
            },
        ),
    ],
)
def test_new_officer_tools_forward_authenticated_requests(
    monkeypatch, tool_name, args, method, path, params, json_body
):
    monkeypatch.setattr(tools, "_access_token", lambda: "officer-token")
    requests = []
    expected_response = {"ok": True}

    def request(self, actual_method, actual_path, token, *, params=None, json_body=None):
        requests.append(
            (actual_method, actual_path, token, params, json_body)
        )
        return expected_response

    monkeypatch.setattr(tools.PortalClient, "request", request)

    result = getattr(tools, tool_name)(**args)

    assert result is expected_response
    assert requests == [(method, path, "officer-token", params, json_body)]


def test_officer_tools_require_authenticated_access_token(monkeypatch):
    monkeypatch.setattr(tools, "get_access_token", lambda: None)

    with pytest.raises(AuthorizationError):
        tools.list_sla_risk_applications()