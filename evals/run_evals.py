import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import jwt
import psycopg2
import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parent.parent
PERSONAS_FILE = ROOT / "evals" / "personas.json"
RESULTS_FILE = Path(os.getenv(
    "EVAL_RESULTS_FILE",
    str(ROOT / "evals" / "results" / "results.json"),
))
CITIZEN_MCP_URL = os.getenv("CITIZEN_MCP_URL", "http://localhost:8001/mcp")
OFFICER_MCP_URL = os.getenv("OFFICER_MCP_URL", "http://localhost:8002/mcp")
EVAL_PORTAL_BASE_URL = os.getenv(
    "EVAL_PORTAL_BASE_URL",
    "http://localhost:8000",
).rstrip("/")
EVAL_SMS_GATEWAY_URL = os.getenv(
    "EVAL_SMS_GATEWAY_URL",
    "http://localhost:8025",
).rstrip("/")

def make_pdf_fixture():
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Resources << >> >>",
    ]
    contents = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(contents))
        contents.extend(
            f"{index} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        )
    xref_offset = len(contents)
    contents.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    contents.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        contents.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    contents.extend(
        (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(contents)


PDF_FIXTURE = make_pdf_fixture()


@asynccontextmanager
async def mcp_session(url, token):
    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (
        read_stream,
        write_stream,
        _,
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


async def call_tool(session, name, arguments=None):
    response = await session.call_tool(name, arguments or {})
    structured = getattr(response, "structuredContent", None)
    if isinstance(structured, dict):
        result = structured
    else:
        texts = [
            block.text
            for block in response.content
            if getattr(block, "text", None) is not None
        ]
        text = "\n".join(texts)
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = {"message": text}
    return result, bool(response.isError)


def add_check(result, label, passed, detail):
    result["checks"].append({
        "check": label,
        "passed": bool(passed),
        "detail": detail,
    })


def token_claims(token):
    claims = jwt.decode(token, options={"verify_signature": False})
    subject = claims.get("sub", "")
    if not subject.startswith("citizen:"):
        raise ValueError("Evaluation citizen token has an unexpected subject.")
    return claims, subject.removeprefix("citizen:")


def _redirect_target(response, base_url, step):
    location = response.headers.get("Location")
    if not response.is_redirect or not location:
        raise RuntimeError("OAuth test sign-in did not continue at %s." % step)
    return urljoin(base_url + "/", location)


def _delivered_otp(mobile, sms_token):
    headers = {"Authorization": "Bearer " + sms_token}
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        response = requests.get(
            EVAL_SMS_GATEWAY_URL + "/api/messages",
            headers=headers,
            params={"to": mobile},
            timeout=5,
        )
        response.raise_for_status()
        messages = response.json().get("messages", [])
        for message in reversed(messages):
            if not message.get("delivered") or not isinstance(message.get("text"), str):
                continue
            match = re.search(r"Your Sewa Setu OTP is (\d{6})", message["text"])
            if match:
                return match.group(1)
        time.sleep(0.2)
    raise RuntimeError("The isolated SMS gateway did not deliver the test OTP.")


def _authorize_test_token(client, role, *, mobile=None, resource):
    issuer = EVAL_PORTAL_BASE_URL
    redirect_uri = "http://localhost/eval-oauth-callback"
    state = secrets.token_urlsafe(18)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    response = requests.get(
        issuer + "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "scope": role,
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "resource": resource,
        },
        allow_redirects=False,
        timeout=10,
    )
    login_url = _redirect_target(response, issuer, "authorization")
    session = requests.Session()
    login_page = session.get(login_url, timeout=10)
    login_page.raise_for_status()

    if role == "citizen":
        if not mobile:
            raise ValueError("A synthetic citizen phone is required for OAuth.")
        response = session.post(
            login_url,
            data={"mobile": mobile},
            allow_redirects=False,
            timeout=10,
        )
        if response.status_code != 200:
            raise RuntimeError("The citizen OAuth OTP request was rejected.")
        sms_token = os.getenv("EVAL_SMS_GATEWAY_TOKEN", "")
        otp = _delivered_otp(mobile, sms_token)
        response = session.post(
            login_url,
            data={"mobile": mobile, "otp": otp},
            allow_redirects=False,
            timeout=10,
        )
    elif role == "officer":
        username = os.getenv("EVAL_ADMIN_USERNAME", "")
        password = os.getenv("EVAL_ADMIN_PASSWORD", "")
        if not username or not password:
            raise ValueError(
                "EVAL_ADMIN_USERNAME and EVAL_ADMIN_PASSWORD are required "
                "for an isolated officer OAuth sign-in."
            )
        response = session.post(
            login_url,
            data={"username": username, "password": password},
            allow_redirects=False,
            timeout=10,
        )
    else:
        raise ValueError("Unsupported evaluation OAuth role.")

    consent_url = _redirect_target(response, issuer, role + " login")
    consent_page = session.get(consent_url, timeout=10)
    consent_page.raise_for_status()
    response = session.post(
        consent_url,
        data={"decision": "allow"},
        allow_redirects=False,
        timeout=10,
    )
    callback_url = _redirect_target(response, issuer, "consent")
    callback_query = parse_qs(urlparse(callback_url).query)
    if callback_query.get("state") != [state] or "code" not in callback_query:
        raise RuntimeError("OAuth consent did not return the expected authorization code.")

    response = requests.post(
        issuer + "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "code": callback_query["code"][0],
            "code_verifier": verifier,
            "resource": resource,
        },
        timeout=10,
    )
    response.raise_for_status()
    token = response.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("OAuth token endpoint returned no access token.")
    claims = jwt.decode(token, options={"verify_signature": False})
    expected_subject = (
        "citizen:" + mobile if role == "citizen"
        else "officer:" + os.getenv("EVAL_ADMIN_USERNAME", "")
    )
    if (
        claims.get("sub") != expected_subject
        or claims.get("role") != role
        or claims.get("scope") != role
        or claims.get("aud") != resource
    ):
        raise RuntimeError("The issued OAuth token has an unexpected test identity or scope.")
    return token


def issue_test_oauth_tokens(personas):
    sms_token = os.getenv("EVAL_SMS_GATEWAY_TOKEN", "")
    if not sms_token:
        raise ValueError("EVAL_SMS_GATEWAY_TOKEN is required for synthetic OTP verification.")
    response = requests.post(
        EVAL_PORTAL_BASE_URL + "/oauth/register",
        json={
            "client_name": "Seva Setu Isolated Evaluation Runner",
            "redirect_uris": ["http://localhost/eval-oauth-callback"],
            "token_endpoint_auth_method": "none",
        },
        timeout=10,
    )
    response.raise_for_status()
    client = response.json()
    citizen_resource = os.getenv(
        "CITIZEN_RESOURCE_URL",
        "http://localhost:8001/mcp",
    ).rstrip("/")
    officer_resource = os.getenv(
        "OFFICER_RESOURCE_URL",
        "http://localhost:8002/mcp",
    ).rstrip("/")

    used_numbers = set()
    citizen_tokens = {}
    for persona in personas:
        while True:
            mobile = str(9000000000 + secrets.randbelow(100000000))
            if mobile not in used_numbers:
                used_numbers.add(mobile)
                break
        citizen_tokens[persona["id"]] = _authorize_test_token(
            client,
            "citizen",
            mobile=mobile,
            resource=citizen_resource,
        )
    officer_token = _authorize_test_token(
        client,
        "officer",
        resource=officer_resource,
    )
    return citizen_tokens, officer_token


def query_one(database_url, query, params):
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def verify_disposable_database(database_url):
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), to_regclass('public.evaluation_sandbox_marker')")
            database_name, marker_table = cur.fetchone()
            if not database_name.endswith(("_eval", "_test")) or marker_table is None:
                raise ValueError(
                    "Refusing to run mutating MCP evaluations outside a marked "
                    "disposable evaluation database."
                )
            cur.execute(
                "SELECT count(*) FROM evaluation_sandbox_marker "
                "WHERE marker = %s",
                ("sewasetu-disposable-evaluation-database-v1",),
            )
            if cur.fetchone()[0] != 1:
                raise ValueError("The evaluation database safety marker is invalid.")
            cur.execute("SELECT count(*) FROM applications")
            existing_applications = cur.fetchone()[0]
            if existing_applications:
                raise ValueError(
                    "Refusing to run in a non-empty evaluation database "
                    "(%d application rows)." % existing_applications
                )


def safe_call_record(tool, result, is_error):
    return {
        "tool": tool,
        "result": result,
        "is_error": is_error,
    }


async def review_synthetic_proof(item, draft_ref, officer_token):
    async with mcp_session(OFFICER_MCP_URL, officer_token) as session:
        review, review_error = await call_tool(
            session,
            "get_age_proof_review_document",
            {"draft_ref": draft_ref},
        )
        item["conversation"].append(
            safe_call_record("get_age_proof_review_document", review, review_error)
        )
        expected_file = base64.b64encode(PDF_FIXTURE).decode("ascii")
        document_matches = (
            not review_error
            and review.get("draft_ref") == draft_ref
            and review.get("document_status") == "REVIEW_REQUIRED"
            and review.get("file_base64") == expected_file
        )
        add_check(
            item,
            "officer MCP retrieves the stored synthetic proof",
            document_matches,
            "Officer review metadata matches uploaded fixture=%s." % document_matches,
        )
        if not document_matches:
            return False

        decision, decision_error = await call_tool(
            session,
            "decide_age_proof_review",
            {
                "draft_ref": draft_ref,
                "document_id": review["document_id"],
                "decision": "VALID",
                "reason": (
                    "Isolated evaluation: reviewed the generated synthetic proof "
                    "fixture for workflow coverage."
                ),
            },
        )
        item["conversation"].append(
            safe_call_record("decide_age_proof_review", decision, decision_error)
        )
        add_check(
            item,
            "authorized officer records the synthetic document review",
            not decision_error and decision.get("document_status") == "VALID",
            "Document review response: %s" % decision,
        )
        return not decision_error and decision.get("document_status") == "VALID"


async def evaluate_persona(persona, token, officer_token, database_url):
    item = {
        "persona_id": persona["id"],
        "persona_name": persona["name"],
        "age": persona["age"],
        "village": persona["village"],
        "block": persona["block"],
        "goal": persona["goal"],
        "opening_message": persona["opening_message"],
        "how_they_talk": persona.get(
            "how_she_talks", persona.get("how_he_talks", "")
        ),
        "expected_portal_state": persona["expected_portal_state"],
        "conversation": [{
            "speaker": "citizen",
            "message": persona["opening_message"],
        }],
        "checks": [],
        "status": "failed",
    }
    _, mobile = token_claims(token)
    before = query_one(
        database_url,
        "SELECT count(*) FROM applications WHERE mobile = %s",
        (mobile,),
    )[0]
    add_check(
        item,
        "synthetic mobile starts with no prior applications",
        before == 0,
        f"Existing application rows before scenario: {before}",
    )
    if before:
        item["note"] = "Use a clean evaluation database and a fresh citizen identity."
        return item

    async with mcp_session(CITIZEN_MCP_URL, token) as session:
        draft, draft_error = await call_tool(
            session,
            "create_or_resume_application_draft",
            {
                "applicant_name": persona["name"],
                "date_of_birth": persona["date_of_birth"],
                "village": persona["village"],
                "block": persona["block"],
                "bank_account": "123456789012",
                "ifsc": "SBIN0003077",
                "gender": "Female",
                "marital_status": "Widowed",
                "husband_name": "Synthetic Test",
            },
        )
        item["conversation"].append(
            safe_call_record("create_or_resume_application_draft", draft, draft_error)
        )
        draft_ref = draft.get("draft_ref")
        add_check(
            item,
            "authenticated citizen can create a draft through MCP",
            not draft_error and bool(draft_ref),
            "Draft response: %s" % draft,
        )
        if not draft_ref:
            item["note"] = "Draft preflight did not return a draft reference."
            return item

        uploaded, upload_error = await call_tool(
            session,
            "attach_age_proof",
            {
                "draft_ref": draft_ref,
                "filename": "synthetic-age-proof.pdf",
                "file_base64": base64.b64encode(PDF_FIXTURE).decode("ascii"),
            },
        )
        item["conversation"].append(
            safe_call_record("attach_age_proof", uploaded, upload_error)
        )
        add_check(
            item,
            "synthetic proof file is attached to the saved draft",
            not upload_error
            and uploaded.get("draft_ref") == draft_ref
            and uploaded.get("document_status") == "PENDING_VALIDATION",
            "Attachment response: %s" % uploaded,
        )
        validated, validation_error = await call_tool(
            session,
            "validate_age_proof",
            {"draft_ref": draft_ref},
        )
        item["conversation"].append(
            safe_call_record("validate_age_proof", validated, validation_error)
        )
        add_check(
            item,
            "readable age-proof is routed to officer review",
            not validation_error
            and validated.get("document_status") == "REVIEW_REQUIRED",
            "Validation response: %s" % validated,
        )
        if upload_error or validation_error:
            item["note"] = "Synthetic age-proof file did not reach officer review."
            return item
        if not await review_synthetic_proof(item, draft_ref, officer_token):
            item["note"] = "The isolated officer proof-review workflow failed."
            return item

        summary, summary_error = await call_tool(
            session,
            "get_application_draft",
            {"draft_ref": draft_ref},
        )
        item["conversation"].append(
            safe_call_record("get_application_draft", summary, summary_error)
        )
        add_check(
            item,
            "citizen can inspect the masked final summary",
            not summary_error
            and summary.get("draft_ref") == draft_ref
            and summary.get("document_status") == "VALID",
            "Final draft summary: %s" % summary,
        )
        submitted, submit_error = await call_tool(
            session,
            "submit_pension_application",
            {
                "draft_ref": draft_ref,
                "declaration_confirmed": True,
                "summary_confirmed": True,
            },
        )
        item["conversation"].append(
            safe_call_record("submit_pension_application", submitted, submit_error)
        )

        if persona["age"] >= 60:
            app_id = submitted.get("application_id")
            add_check(
                item,
                "eligible %s application accepted" % persona["block"],
                not submit_error and bool(app_id)
                and submitted.get("status") == "PENDING",
                "Portal tool response: %s" % submitted,
            )
            if not app_id:
                item["note"] = "Submission did not return a portal application id."
                return item
            dbrow = query_one(
                database_url,
                "SELECT mobile, status, block, doc_path FROM applications WHERE id = %s",
                (app_id,),
            )
            add_check(
                item,
                "portal database owns the application and age-proof file",
                bool(dbrow)
                and dbrow[0] == mobile
                and dbrow[1] == "PENDING"
                and dbrow[2] == persona["block"]
                and bool(dbrow[3])
                and os.path.isfile(dbrow[3]),
                "Portal row: %s" % (dbrow,),
            )
            submit_audit = query_one(
                database_url,
                "SELECT action FROM audit_log WHERE application_id = %s "
                "AND action = 'SUBMIT'",
                (app_id,),
            )
            add_check(
                item,
                "application submission is present in the audit trail",
                submit_audit == ("SUBMIT",),
                f"Submission audit row: {submit_audit}",
            )

            duplicate, duplicate_error = await call_tool(
                session,
                "create_or_resume_application_draft",
            )
            item["conversation"].append(
                safe_call_record(
                    "create_or_resume_application_draft (duplicate)",
                    duplicate,
                    duplicate_error,
                )
            )
            count = query_one(
                database_url,
                "SELECT count(*) FROM applications WHERE mobile = %s",
                (mobile,),
            )[0]
            add_check(
                item,
                "duplicate active application is denied",
                not duplicate_error and duplicate.get("application_exists") is True
                and count == 1,
                "Duplicate preflight=%s; portal row count=%d."
                % (duplicate, count),
            )

            status, status_error = await call_tool(session, "check_my_application")
            item["conversation"].append(
                safe_call_record("check_my_application", status, status_error)
            )
            add_check(
                item,
                "citizen can follow the portal application",
                not status_error
                and status.get("application_number") == submitted.get("application_number"),
                "Status response: %s" % status,
            )
            item["officer_application_id"] = app_id
            item["officer_mobile"] = mobile
        else:
            no_application = query_one(
                database_url,
                "SELECT count(*) FROM applications WHERE mobile = %s",
                (mobile,),
            )[0] == 0
            expected_error = (
                submit_error
                and "60 years" in json.dumps(submitted).lower()
            )
            add_check(
                item,
                "below-minimum-age application is rejected by backend validation",
                expected_error and no_application,
                "Portal response=%s; no application row=%s."
                % (submitted, no_application),
            )

    item["status"] = (
        "passed" if all(check["passed"] for check in item["checks"]) else "failed"
    )
    return item


async def evaluate_officer(item, token, database_url):
    claims = jwt.decode(token, options={"verify_signature": False})
    subject = claims.get("sub", "")
    if not subject.startswith("officer:"):
        raise ValueError("Evaluation officer token has an unexpected subject.")
    officer = subject.removeprefix("officer:")
    app_id = item["officer_application_id"]
    reason = "Synthetic evaluation: required fields and age-proof document reviewed."

    async with mcp_session(OFFICER_MCP_URL, token) as session:
        queue, queue_error = await call_tool(
            session,
            "list_pending_applications",
            {"limit": 20},
        )
        item["conversation"].append(
            safe_call_record("list_pending_applications", queue, queue_error)
        )
        queue_ids = {
            row.get("application_id")
            for row in queue.get("applications", [])
        }
        add_check(
            item,
            "officer queue includes the new pending %s case" % item["block"],
            not queue_error and app_id in queue_ids,
            "Case %s listed=%s." % (app_id, app_id in queue_ids),
        )

        case, case_error = await call_tool(
            session,
            "review_application",
            {"application_id": app_id},
        )
        item["conversation"].append(
            safe_call_record("review_application", case, case_error)
        )
        add_check(
            item,
            "officer can review case details and submission audit",
            not case_error
            and case.get("application_id") == app_id
            and case.get("age_proof_available")
            and any(
                audit.get("action") == "SUBMIT"
                for audit in case.get("audit_trail", [])
            ),
            "Review returned case, proof availability and audit trail.",
        )

        decision, decision_error = await call_tool(
            session,
            "decide_on_application",
            {
                "application_id": app_id,
                "decision": "approve",
                "reason": reason,
            },
        )
        item["conversation"].append(
            safe_call_record("decide_on_application", decision, decision_error)
        )
        row = query_one(
            database_url,
            "SELECT status, decided_by FROM applications WHERE id = %s",
            (app_id,),
        )
        audit = query_one(
            database_url,
            "SELECT action, actor, note FROM audit_log "
            "WHERE application_id = %s AND action = 'APPROVED' ORDER BY at DESC LIMIT 1",
            (app_id,),
        )
        add_check(
            item,
            "officer decision and reason are recorded against the officer",
            not decision_error
            and row == ("APPROVED", officer)
            and audit == ("APPROVED", "officer:" + officer, reason),
            "Application row=%s; audit row=%s." % (row, audit),
        )
        citizen_token = item.pop("evaluation_citizen_token", None)
        if citizen_token:
            async with mcp_session(CITIZEN_MCP_URL, citizen_token) as citizen_session:
                status, status_error = await call_tool(
                    citizen_session,
                    "check_my_application",
                )
                item["conversation"].append(
                    safe_call_record("check_my_application after officer decision", status, status_error)
                )
                add_check(
                    item,
                    "citizen sees officer decision through same portal record",
                    not status_error and status.get("status") == "APPROVED",
                    "Citizen status response: %s" % status,
                )

    item["status"] = (
        "passed" if all(check["passed"] for check in item["checks"]) else "failed"
    )


def write_results(results, status, note):
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "evaluation_status": status,
        "note": note,
        "results": results,
    }
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = RESULTS_FILE.with_suffix(RESULTS_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(output, indent=2), encoding="utf-8")
    os.replace(temporary, RESULTS_FILE)


def main():
    personas = json.loads(PERSONAS_FILE.read_text(encoding="utf-8"))
    records = [
        {
            "persona_id": persona["id"],
            "persona_name": persona["name"],
            "age": persona["age"],
            "village": persona["village"],
            "block": persona["block"],
            "goal": persona["goal"],
            "opening_message": persona["opening_message"],
            "how_they_talk": persona.get(
                "how_she_talks", persona.get("how_he_talks", "")
            ),
            "expected_portal_state": persona["expected_portal_state"],
            "conversation": [],
            "checks": [],
            "status": "not_run",
        }
        for persona in personas
    ]
    try:
        citizen_tokens = json.loads(os.getenv("EVAL_CITIZEN_TOKENS") or "{}")
        officer_token = os.getenv("EVAL_OFFICER_TOKEN", "")
        database_url = os.getenv("EVAL_DATABASE_URL", "")
        if not isinstance(citizen_tokens, dict):
            raise ValueError("EVAL_CITIZEN_TOKENS must be a JSON object.")
        missing = []
        if not database_url:
            missing.append("EVAL_DATABASE_URL")
        if database_url:
            verify_disposable_database(database_url)
        if (
            database_url
            and os.getenv("EVAL_AUTO_OAUTH", "").lower() == "true"
            and (
                not officer_token
                or any(not citizen_tokens.get(persona["id"]) for persona in personas)
            )
        ):
            citizen_tokens, officer_token = issue_test_oauth_tokens(personas)
        if not officer_token:
            missing.append("EVAL_OFFICER_TOKEN")
        missing_personas = [
            persona["id"] for persona in personas if not citizen_tokens.get(persona["id"])
        ]
        if missing_personas:
            missing.append("citizen tokens for: " + ", ".join(missing_personas))
        if missing:
            write_results(
                records,
                "not_run",
                "Real-client evals require a clean evaluation database and "
                "fresh OAuth tokens. Missing: " + "; ".join(missing),
            )
            print("Evals not run: " + "; ".join(missing))
            return 2
        async def run():
            evaluated = []
            for persona in personas:
                record = await evaluate_persona(
                    persona,
                    citizen_tokens[persona["id"]],
                    officer_token,
                    database_url,
                )
                if record.get("officer_application_id"):
                    record["evaluation_citizen_token"] = citizen_tokens[persona["id"]]
                    await evaluate_officer(record, officer_token, database_url)
                evaluated.append(record)
            return evaluated

        results = asyncio.run(run())
        passed = sum(item["status"] == "passed" for item in results)
        status = "passed" if passed == len(results) else "failed"
        write_results(
            results,
            status,
            f"{passed} of {len(results)} persona evaluations passed using MCP "
            "streamable-HTTP clients and direct portal database assertions.",
        )
        print(f"Evaluation status: {status} ({passed}/{len(results)} passed)")
        print(f"Results written to: {RESULTS_FILE}")
        return 0 if status == "passed" else 1
    except Exception as exc:
        write_results(
            records,
            "failed",
            "Evaluation could not complete (%s): %s" % (type(exc).__name__, exc),
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
