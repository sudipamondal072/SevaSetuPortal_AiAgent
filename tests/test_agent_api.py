import base64
import datetime
import io
import logging
import os
import sys
import threading
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from PIL import Image
from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

import app as portal
import agent_api
import oauth


@pytest.fixture
def client():
    return portal.app.test_client()


def test_get_db_uses_environment_overrides(monkeypatch):
    connection_kwargs = {}

    def connect(**kwargs):
        connection_kwargs.update(kwargs)
        return object()

    monkeypatch.setenv("DB_PASSWORD", "test-database-password")
    monkeypatch.setenv("DB_HOST", "test-db-host")
    monkeypatch.setenv("DB_PORT", "55432")
    monkeypatch.setenv("DB_NAME", "sewasetu_eval")
    monkeypatch.setenv("DB_USER", "test-db-user")
    monkeypatch.setattr(portal.psycopg2, "connect", connect)

    portal.get_db()

    assert connection_kwargs == {
        "host": "test-db-host",
        "port": "55432",
        "dbname": "sewasetu_eval",
        "user": "test-db-user",
        "password": "test-database-password",
    }


def valid_pdf_bytes():
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def valid_png_bytes():
    result = io.BytesIO()
    Image.new("RGB", (2, 2), color="white").save(result, format="PNG")
    return result.getvalue()


def citizen_claims():
    return {
        "sub": "citizen:9000000001",
        "role": "citizen",
        "scope": "citizen",
    }


def officer_claims():
    return {
        "sub": "officer:admin",
        "role": "officer",
        "scope": "officer",
    }


def application_payload(**overrides):
    payload = {
        "applicant_name": "Kamla Devi",
        "dob": "12/04/1956",
        "village": "Sonari Basti",
        "block": "Sonari",
        "bank_account": "123456789012",
        "ifsc": "SBIN0003077",
        "age_proof_filename": "proof.pdf",
        "age_proof_base64": base64.b64encode(b"synthetic-proof").decode("ascii"),
        "declaration_confirmed": True,
    }
    payload.update(overrides)
    return payload


def test_scheme_information_ignores_legacy_sonari_only_setting(
    client, monkeypatch, caplog
):
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")

    with caplog.at_level(logging.INFO, logger=portal.app.logger.name):
        response = client.get("/api/agent/scheme")

    assert response.status_code == 200
    assert response.json["enabled_blocks"] == portal.BLOCKS
    assert "MCP_ENABLED_BLOCKS is deprecated and ignored" in caplog.text
    assert response.json["application_form_url"].endswith("/apply")
    assert response.json["monthly_amount_inr"] == 250
    assert response.json["bank_account_accepted_lengths"] == list(
        portal.BANK_ACCOUNT_LENGTHS
    )
    assert "agent_request route=/api/agent/scheme" in caplog.text
    assert "total_ms=" in caplog.text
    assert "db_queries=0" in caplog.text


def test_application_submission_form_has_accessible_processing_indicator():
    with portal.app.test_request_context():
        rendered = portal.app.jinja_env.get_template("declaration.html").render(
            data={},
            doc_name="proof.pdf",
            errors=[],
            upload_error=None,
            editing=False,
            edit_used=True,
        )

    assert 'data-processing-message="Submitting your application securely.' in rendered
    assert 'class="processing" hidden role="status" aria-live="polite"' in rendered
    assert "processing-text" in rendered


def test_citizen_draft_schema_migration_runs_once_after_commit(monkeypatch):
    statements = []

    class Connection:
        commits = 0

        def commit(self):
            self.commits += 1

    class Cursor:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, query):
            statements.append(query)

    connection = Connection()
    cursor = Cursor(connection)
    monkeypatch.setattr(agent_api, "_CITIZEN_DRAFT_SCHEMA_READY", False)

    agent_api._ensure_citizen_drafts_table(cursor)
    initial_statement_count = len(statements)
    agent_api._ensure_citizen_drafts_table(cursor)

    assert initial_statement_count == 5
    assert len(statements) == initial_statement_count
    assert connection.commits == 1


def test_timed_cursor_logs_query_duration_without_parameters(caplog):
    class Cursor:
        def execute(self, _query, _params=None):
            pass

        def fetchone(self):
            return None

    caplog.set_level(logging.INFO, logger=portal.app.logger.name)
    with portal.app.test_request_context("/api/agent/citizen/application"):
        from flask import g

        g.agent_db_queries = 0
        g.agent_db_ms = 0.0
        cursor = agent_api._TimedCursor(Cursor())
        cursor.execute(
            "SELECT * FROM applications WHERE mobile = %s",
            ("private-test-value",),
        )
        cursor.fetchone()
        query_count = g.agent_db_queries
        total_db_ms = g.agent_db_ms

    assert query_count == 1
    assert total_db_ms >= 0
    assert "agent_db_step" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "private-test-value" not in caplog.text
    assert "SELECT * FROM applications" not in caplog.text


def test_enabled_blocks_always_uses_portal_catalog(monkeypatch):
    for configured in ("", "Sonari", "Not a portal block"):
        monkeypatch.setenv("MCP_ENABLED_BLOCKS", configured)
        assert agent_api.enabled_blocks() == portal.BLOCKS


def test_unknown_village_mapping_is_not_a_validation_error():
    payload = application_payload(village="Unmapped hamlet")

    errors, cleaned = portal.validate_application(payload, "/private/proof.pdf")

    assert not any("village" in error.lower() for error in errors)
    assert cleaned["village"] == "Unmapped hamlet"


def test_bank_account_spaces_are_removed_and_leading_zeroes_preserved():
    payload = application_payload(bank_account=" 00 1234567 ")

    errors, cleaned = portal.validate_application(payload, "/private/proof.pdf")

    assert not any("bank account" in error.lower() for error in errors)
    assert cleaned["bank_account"] == "001234567"


def test_bank_account_validation_accepts_ascii_digits_only():
    errors, _ = portal.validate_application(
        application_payload(bank_account="١٢٣٤٥٦٧٨٩"), "/private/proof.pdf"
    )
    updates, error = agent_api._clean_draft_updates({"bank_account": "١٢٣٤٥٦٧٨٩"})

    assert any("digits only" in item for item in errors)
    assert updates is None
    assert "digits only" in error


def test_otp_verification_initializes_server_side_web_draft(client, monkeypatch):
    class Cursor:
        def execute(self, *_args):
            pass

        def fetchone(self):
            return ("123456", datetime.datetime.now())

        def close(self):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(portal, "get_db", Connection)
    monkeypatch.setattr(
        portal,
        "get_or_create_web_draft",
        lambda mobile: ("draft-verified", True, False, False),
    )
    with client.session_transaction() as flask_session:
        flask_session["apply_mobile"] = "9000000001"

    response = client.post("/verify", data={"otp": "123456"})

    assert response.status_code == 302
    with client.session_transaction() as flask_session:
        assert flask_session["web_draft_id"] == "draft-verified"
        assert flask_session["web_reviewed"] is True
        assert flask_session["web_edit_used"] is False


@pytest.mark.parametrize("bank_account", ["12345678", "1234567890123456789", "12A456789"])
def test_bank_account_invalid_length_or_characters_are_rejected(bank_account):
    payload = application_payload(bank_account=bank_account)

    errors, _ = portal.validate_application(payload, "/private/proof.pdf")

    assert any("bank account number" in error for error in errors)


def test_duplicate_check_locks_mobile_and_ignores_rejected_or_withdrawn_history():
    class Cursor:
        statements = []

        def execute(self, query, params):
            self.statements.append((query, params))

        def fetchone(self):
            return None

    cursor = Cursor()

    portal.active_application(cursor, "9000000001")

    assert "pg_advisory_xact_lock" in cursor.statements[0][0]
    application_query = cursor.statements[1][0]
    assert "WHERE mobile = %s AND" in application_query
    assert "status IS NULL OR status NOT IN ('REJECTED', 'WITHDRAWN')" in application_query


def test_withdrawn_application_status_allows_new_application(client, monkeypatch):
    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query

        def fetchone(self):
            if "FROM applications" in self.last_query:
                return (
                    42, "SSP12345678", "WITHDRAWN", "Sonari",
                    None, datetime.datetime(2026, 10, 23), "APPLICANT",
                )
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: Connection())

    response = client.get(
        "/api/agent/citizen/application",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    assert response.json["status"] == "WITHDRAWN"
    assert response.json["can_apply_again"] is True


def test_new_draft_replaces_completed_draft_after_withdrawal(client, monkeypatch):
    old_doc_path = "/private/uploads/citizen_draft_old.pdf"
    statements = []

    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query
            statements.append(query)

        def fetchone(self):
            if "FROM applications" in self.last_query:
                return None
            if "SELECT draft_ref, draft_data, doc_path, doc_filename" in self.last_query:
                return (
                    "previous-draft-ref",
                    {"applicant_name": "Old Application"},
                    old_doc_path,
                    "proof.pdf",
                    "VALID",
                    None,
                    True,
                )
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    removed_files = []
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: Connection())
    monkeypatch.setattr(agent_api, "_remove_expired_draft_file", removed_files.append)

    response = client.post(
        "/api/agent/citizen/application-drafts",
        headers={"Authorization": "Bearer test-token"},
        json={"applicant_name": "New Application"},
    )

    assert response.status_code == 201
    assert response.json["draft_ref"] != "previous-draft-ref"
    assert response.json["details"]["applicant_name"] == "New Application"
    assert response.json["document_status"] == "PENDING_UPLOAD"
    assert response.json["draft_status"] == "DRAFT"
    assert removed_files == [old_doc_path]
    assert any("DELETE FROM citizen_draft_reviews" in query for query in statements)
    assert any(
        "doc_path = NULL, doc_id = NULL" in query and "completed = FALSE" in query
        for query in statements
    )


def test_create_draft_preflight_returns_existing_application_without_extra_reads(
    client, monkeypatch
):
    statements = []

    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query
            statements.append(query)

        def fetchone(self):
            if "FROM applications" in self.last_query:
                return (42, "SSP12345678", "APPROVED")
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "_CITIZEN_DRAFT_SCHEMA_READY", True)
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: Connection())

    response = client.post(
        "/api/agent/citizen/application-drafts",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )

    assert response.status_code == 200
    assert response.json["application_exists"] is True
    assert response.json["application_number"] == "SSP12345678"
    assert response.json["status"] == "APPROVED"
    assert response.json["action"] == "existing_application"
    assert not any("citizen_application_drafts" in query for query in statements)


def test_simultaneous_application_checks_are_serialized_by_mobile_lock():
    lock = threading.Lock()
    state = {"application": None}
    created = []
    results = []
    start = threading.Barrier(2)

    class Cursor:
        last_query = ""

        def execute(self, query, params):
            self.last_query = query
            if "pg_advisory_xact_lock" in query:
                lock.acquire()

        def fetchone(self):
            if "FROM applications" in self.last_query:
                return state["application"]
            return None

    def submit_attempt():
        start.wait()
        cursor = Cursor()
        try:
            existing = portal.active_application(cursor, "9000000001")
            if existing:
                results.append(existing[2])
                return
            state["application"] = (1, "SSP12345678", "PENDING")
            created.append(state["application"])
            results.append(None)
        finally:
            lock.release()

    workers = [
        threading.Thread(target=submit_attempt),
        threading.Thread(target=submit_attempt),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert len(created) == 1
    assert sorted(results, key=lambda value: value or "") == [None, "PENDING"]


def test_bank_account_masking_never_reveals_short_or_full_values():
    assert portal.mask_bank_account("000123456") == "*****3456"
    assert portal.mask_bank_account("1234") == "****"
    assert portal.mask_sensitive_numbers("Account 123456789012") == "Account ********9012"
    assert agent_api._draft_response((
        "draft-ref",
        {"bank_account": "000123456"},
        None,
        None,
        "PENDING_UPLOAD",
        None,
        False,
    ))["details"]["bank_account"] == "*****3456"


def test_application_requires_explicit_declaration(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/applications",
        headers={"Authorization": "Bearer signed-token"},
        json=application_payload(declaration_confirmed=False),
    )

    assert response.status_code == 400
    assert "declaration" in response.json["error"]


def test_citizen_withdrawal_requires_explicit_confirmation(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/applications/1/withdraw",
        headers={"Authorization": "Bearer citizen-token"},
        json={"confirm": False},
    )

    assert response.status_code == 400
    assert "confirmation" in response.json["error"]


def test_application_slip_requires_citizen_authentication(client, monkeypatch):
    monkeypatch.setattr(
        agent_api,
        "verify_token",
        lambda token, audience: None,
    )

    response = client.get("/api/agent/citizen/application/slip")

    assert response.status_code == 401


def test_citizen_can_download_own_application_slip(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            assert "WHERE mobile = %s" in query
            assert params == ("9000000001",)

        def fetchone(self):
            return (42, "SSP12345678")

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: Connection())
    monkeypatch.setattr(portal, "generate_acknowledgment", lambda cur, app_id: b"%PDF-slip")

    response = client.get(
        "/api/agent/citizen/application/slip",
        headers={"Authorization": "Bearer citizen-token"},
    )

    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.headers["Content-Disposition"] == (
        'attachment; filename=SSP12345678.pdf'
    )
    assert response.data == b"%PDF-slip"


def test_citizen_can_withdraw_own_pending_application_and_audit_it(client, monkeypatch):
    audit_entries = []

    class Cursor:
        last_query = ""
        last_params = ()

        def execute(self, query, params):
            self.last_query = query
            self.last_params = params
            if query.startswith("INSERT INTO audit_log"):
                audit_entries.append(params)

        def fetchone(self):
            if "UPDATE applications" in self.last_query:
                assert self.last_params[1:] == (42, "9000000001")
                return ("SSP12345678",)
            return None

    class Connection:
        committed = False
        rolled_back = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "now_ist", lambda: datetime.datetime(2026, 10, 24))

    response = client.post(
        "/api/agent/citizen/applications/42/withdraw",
        headers={"Authorization": "Bearer citizen-token"},
        json={"confirm": True},
    )

    assert response.status_code == 200
    assert response.json["application_id"] == 42
    assert response.json["application_number"] == "SSP12345678"
    assert response.json["status"] == "WITHDRAWN"
    assert connection.committed is True
    assert connection.rolled_back is False
    assert audit_entries[0][:4] == (42, "WITHDRAW", "applicant:9000000001", "")
    assert isinstance(audit_entries[0][4], datetime.datetime)


def test_citizen_cannot_withdraw_missing_or_non_pending_application(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            pass

        def fetchone(self):
            return None

    class Connection:
        rolled_back = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)

    response = client.post(
        "/api/agent/citizen/applications/42/withdraw",
        headers={"Authorization": "Bearer citizen-token"},
        json={"confirm": True},
    )

    assert response.status_code == 409
    assert connection.rolled_back is True


def test_underage_applicant_is_rejected_by_portal_validation(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/applications",
        headers={"Authorization": "Bearer signed-token"},
        json=application_payload(dob="05/10/1967"),
    )

    assert response.status_code == 400
    assert any("60 years" in error for error in response.json["validation_errors"])


def test_agent_submission_rejects_block_outside_portal_catalog(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/applications",
        headers={"Authorization": "Bearer signed-token"},
        json=application_payload(block="Unsupported Block"),
    )

    assert response.status_code == 400
    assert response.json["enabled_blocks"] == portal.BLOCKS


def test_direct_submission_cannot_bypass_saved_draft_and_document_review(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/applications",
        headers={"Authorization": "Bearer test-token"},
        json=application_payload(),
    )

    assert response.status_code == 409
    assert "draft workflow" in response.json["error"]


def test_invalid_document_signature_is_rejected_before_storage(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/application-drafts/draft-ref/age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={
            "filename": "proof.pdf",
            "file_base64": base64.b64encode(b"not a PDF").decode("ascii"),
        },
    )

    assert response.status_code == 400
    assert "content does not match" in response.json["error"]


def test_unreadable_file_with_valid_signature_is_rejected(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/application-drafts/draft-ref/age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={
            "filename": "proof.pdf",
            "file_base64": base64.b64encode(b"%PDF-not-a-readable-pdf").decode("ascii"),
        },
    )

    assert response.status_code == 400
    assert "cannot be opened" in response.json["error"]


def test_oversized_age_proof_is_rejected_before_decoding(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    oversized_base64 = "A" * (((agent_api.MAX_DOCUMENT_BYTES + 2) // 3) * 4 + 4)

    response = client.post(
        "/api/agent/citizen/application-drafts/draft-ref/age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={"filename": "proof.pdf", "file_base64": oversized_base64},
    )

    assert response.status_code == 413
    assert "5 MB or smaller" in response.json["error"]


def test_unsupported_age_proof_extension_is_rejected(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.post(
        "/api/agent/citizen/application-drafts/draft-ref/age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={"filename": "proof.txt", "file_base64": base64.b64encode(b"data").decode()},
    )

    assert response.status_code == 400
    assert "PDF, JPG or PNG" in response.json["error"]


def test_later_age_proof_upload_attaches_to_authenticated_draft(
    client, monkeypatch, tmp_path
):
    class Cursor:
        last_query = ""
        last_params = ()

        def execute(self, query, params=None):
            self.last_query = query
            self.last_params = params

        def fetchone(self):
            if "SELECT draft_ref, draft_data" in self.last_query:
                return (
                    "opaque-draft-ref",
                    {"applicant_name": "Kamla Devi"},
                    None,
                    None,
                    "PENDING_UPLOAD",
                    None,
                    False,
                )
            return None

    class Connection:
        committed = False
        rolled_back = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    png_file = valid_png_bytes()
    readability_checks = []
    original_readability_check = portal.proof_content_is_readable

    def count_readability_check(extension, contents):
        readability_checks.append((extension, len(contents)))
        return original_readability_check(extension, contents)

    monkeypatch.setattr(
        portal,
        "proof_content_is_readable",
        count_readability_check,
    )

    response = client.post(
        "/api/agent/citizen/application-drafts/opaque-draft-ref/age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={
            "filename": "proof.png",
            "file_base64": base64.b64encode(png_file).decode("ascii"),
        },
    )

    assert response.status_code == 201
    assert response.json["document_status"] == "PENDING_VALIDATION"
    assert str(tmp_path) not in response.get_data(as_text=True)
    stored = list(tmp_path.iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes() == png_file
    assert connection.committed is True
    assert len(readability_checks) == 1


def test_authenticated_multipart_upload_stores_document_and_returns_pending_status(
    client, monkeypatch, tmp_path
):
    statements = []

    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query
            statements.append((query, params))

        def fetchone(self):
            if "SELECT draft_ref, draft_data, doc_path" in self.last_query:
                return (
                    "private-draft",
                    {"block": "Sonari"},
                    None,
                    None,
                    "PENDING_UPLOAD",
                    None,
                    False,
                    None,
                )
            return None

    class Connection:
        committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    content = valid_pdf_bytes()

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/age-proof",
        headers={"Authorization": "Bearer test-token"},
        data={"document": (io.BytesIO(content), "../../proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    assert response.json["document_status"] == "PENDING_VALIDATION"
    assert response.json["document_id"]
    assert str(tmp_path) not in response.get_data(as_text=True)
    stored_files = list(tmp_path.iterdir())
    assert len(stored_files) == 1
    assert stored_files[0].name.startswith("citizen_draft_")
    assert stored_files[0].read_bytes() == content
    assert connection.committed is True
    assert any(
        "document_status = 'PENDING_VALIDATION'" in query
        for query, _ in statements
    )


def test_draft_upload_cannot_access_another_citizens_draft(client, monkeypatch):
    class Cursor:
        def execute(self, query, params=None):
            pass

        def fetchone(self):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: Connection())

    response = client.post(
        "/api/agent/citizen/application-drafts/someone-elses-draft/age-proof",
        headers={"Authorization": "Bearer test-token"},
        data={"document": (io.BytesIO(valid_pdf_bytes()), "proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 404
    assert "authenticated citizen" in response.json["error"]


def test_upload_storage_failure_preserves_draft_and_returns_retryable_error(
    client, monkeypatch, tmp_path
):
    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query

        def fetchone(self):
            if "SELECT draft_ref, draft_data, doc_path" in self.last_query:
                return (
                    "private-draft", {}, None, None, "PENDING_UPLOAD",
                    None, False, None,
                )
            return None

    class Connection:
        rolled_back = False
        committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    original_open = os.open

    def fail_document_write(path, *args, **kwargs):
        if str(path).startswith(str(tmp_path)):
            raise PermissionError("storage unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    monkeypatch.setattr(agent_api.os, "open", fail_document_write)

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/age-proof",
        headers={"Authorization": "Bearer test-token"},
        data={"document": (io.BytesIO(valid_pdf_bytes()), "proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 503
    assert "draft is unchanged" in response.json["error"]
    assert connection.rolled_back is True
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(agent_api.os, "open", original_open)
    retry = client.post(
        "/api/agent/citizen/application-drafts/private-draft/age-proof",
        headers={"Authorization": "Bearer test-token"},
        data={"document": (io.BytesIO(valid_pdf_bytes()), "proof.pdf")},
        content_type="multipart/form-data",
    )
    assert retry.status_code == 201
    assert retry.json["document_status"] == "PENDING_VALIDATION"
    assert connection.committed is True


def test_validate_age_proof_marks_readable_upload_for_officer_review(
    client, monkeypatch, tmp_path
):
    document_path = tmp_path / "citizen_draft_doc.pdf"
    document_path.write_bytes(valid_pdf_bytes())

    class Cursor:
        last_query = ""
        statements = []

        def execute(self, query, params=None):
            self.last_query = query
            self.statements.append(query)

        def fetchone(self):
            if "SELECT draft_ref, draft_data, doc_path" in self.last_query:
                return (
                    "private-draft", {}, str(document_path), "proof.pdf",
                    "PENDING_VALIDATION", "Document received.", False, "doc-id",
                )
            return None

    class Connection:
        committed = False

        def __init__(self):
            self.db_cursor = Cursor()

        def cursor(self):
            return self.db_cursor

        def commit(self):
            self.committed = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/validate-age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )

    assert response.status_code == 200
    assert response.json["document_status"] == "REVIEW_REQUIRED"
    assert connection.committed is True
    assert any(
        "document_status = 'REVIEW_REQUIRED'" in query
        for query in connection.db_cursor.statements
    )


def test_validate_age_proof_marks_corrupt_stored_file_invalid(
    client, monkeypatch, tmp_path
):
    document_path = tmp_path / "citizen_draft_corrupt.pdf"
    document_path.write_bytes(b"%PDF-not-readable")

    class Cursor:
        last_query = ""
        statements = []

        def execute(self, query, params=None):
            self.last_query = query
            self.statements.append(query)

        def fetchone(self):
            if "SELECT draft_ref, draft_data, doc_path" in self.last_query:
                return (
                    "private-draft", {}, str(document_path), "proof.pdf",
                    "PENDING_VALIDATION", "Document received.", False, "doc-id",
                )
            return None

    class Connection:
        def __init__(self):
            self.db_cursor = Cursor()

        def cursor(self):
            return self.db_cursor

        def commit(self):
            pass

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/validate-age-proof",
        headers={"Authorization": "Bearer test-token"},
        json={},
    )

    assert response.status_code == 200
    assert response.json["document_status"] == "INVALID"
    assert any(
        "document_status = 'INVALID'" in query
        for query in connection.db_cursor.statements
    )


def test_submission_is_blocked_until_document_is_validated_by_officer(
    client, monkeypatch
):
    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query

        def fetchone(self):
            if "SELECT draft_ref, draft_data" in self.last_query:
                return (
                    "opaque-draft-ref",
                    application_payload(),
                    "/private/proof.pdf",
                    "proof.pdf",
                    "REVIEW_REQUIRED",
                    "Officer review required.",
                    False,
                    None,
                    0.0,
                )
            return None

    class Connection:
        rolled_back = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)

    response = client.post(
        "/api/agent/citizen/application-drafts/opaque-draft-ref/submit",
        headers={"Authorization": "Bearer test-token"},
        json={"declaration_confirmed": True, "summary_confirmed": True},
    )

    assert response.status_code == 409
    assert response.json["document_status"] == "REVIEW_REQUIRED"
    assert connection.rolled_back is True


def test_retried_submission_returns_existing_application_without_inserting(
    client, monkeypatch
):
    statements = []
    create_calls = []

    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query
            statements.append(query)

        def fetchone(self):
            if "SELECT draft_ref, draft_data" in self.last_query:
                return (
                    "private-draft",
                    application_payload(),
                    "/private/citizen_draft_proof.pdf",
                    "proof.pdf",
                    "VALID",
                    "Officer verified the age proof.",
                    False,
                    "current-doc-id",
                    0.0,
                )
            if "FROM applications" in self.last_query:
                return (123, "SSP12345678", "PENDING")
            return None

    class Connection:
        rolled_back = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(
        portal,
        "create_application",
        lambda *args: create_calls.append(args),
    )
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/submit",
        headers={"Authorization": "Bearer test-submission-token"},
        json={"declaration_confirmed": True, "summary_confirmed": True},
    )

    assert response.status_code == 200
    assert response.json["application_number"] == "SSP12345678"
    assert response.json["status"] == "PENDING"
    assert connection.rolled_back is True
    assert create_calls == []
    assert sum("pg_advisory_xact_lock" in query for query in statements) == 1


def test_validated_draft_submits_supported_non_sonari_block(
    client, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=portal.app.logger.name)
    statements = []

    class Cursor:
        last_query = ""

        def execute(self, query, params=None):
            self.last_query = query
            statements.append(query)

        def fetchone(self):
            if "SELECT draft_ref, draft_data, doc_path" in self.last_query:
                return (
                    "private-draft",
                    application_payload(block="Rajapara"),
                    "/private/citizen_draft_proof.pdf",
                    "proof.pdf",
                    "VALID",
                    "Age proof verified by an authorized officer.",
                    False,
                    "current-doc-id",
                    0.0,
                )
            return None

    class Connection:
        committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "valid_uploaded_proof", lambda path: True)
    monkeypatch.setattr(
        portal,
        "validate_application",
        lambda data, path: ([], {"block": "Rajapara"}),
    )
    monkeypatch.setattr(portal, "create_application", lambda cur, mobile, cleaned: (123, "SSP12345678"))
    monkeypatch.setattr(portal, "send_sms", lambda mobile, message: None)
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")

    response = client.post(
        "/api/agent/citizen/application-drafts/private-draft/submit",
        headers={"Authorization": "Bearer test-token"},
        json={"declaration_confirmed": True, "summary_confirmed": True},
    )

    assert response.status_code == 201
    assert response.json["application_number"] == "SSP12345678"
    assert response.json["status"] == "PENDING"
    assert connection.committed is True
    assert any("SET completed = TRUE" in query for query in statements)
    assert sum(
        "pg_advisory_xact_lock" in query for query in statements
    ) == 1
    assert "application_lifecycle outcome=submitted total_ms=" in caplog.text


def test_officer_can_mark_only_the_current_document_as_valid(client, monkeypatch):
    class Cursor:
        last_query = ""
        statements = []

        def execute(self, query, params=None):
            self.last_query = query
            self.statements.append((query, params))

        def fetchone(self):
            if "SELECT mobile, draft_data, doc_path" in self.last_query:
                return (
                    "9000000001",
                    {"block": "Sonari"},
                    "/private/citizen_draft_proof.pdf",
                    "REVIEW_REQUIRED",
                    "current-document-id",
                )
            return None

    class Connection:
        committed = False
        rolled_back = False

        def __init__(self):
            self.db_cursor = Cursor()

        def cursor(self):
            return self.db_cursor

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    monkeypatch.setattr(portal, "valid_uploaded_proof", lambda path: True)
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")

    stale_response = client.post(
        "/api/agent/officer/age-proof-review-drafts/draft-ref/decision",
        headers={"Authorization": "Bearer test-token"},
        json={
            "document_id": "stale-document-id",
            "decision": "VALID",
            "reason": "Checked",
        },
    )
    assert stale_response.status_code == 404
    assert connection.rolled_back is True

    connection = Connection()
    monkeypatch.setattr(portal, "get_db", lambda: connection)
    response = client.post(
        "/api/agent/officer/age-proof-review-drafts/draft-ref/decision",
        headers={"Authorization": "Bearer test-token"},
        json={
            "document_id": "current-document-id",
            "decision": "VALID",
            "reason": "Name and age evidence verified against the document.",
        },
    )

    assert response.status_code == 200
    assert response.json["document_status"] == "VALID"
    assert connection.committed is True
    assert any(
        query.startswith("INSERT INTO citizen_draft_reviews")
        for query, _ in connection.db_cursor.statements
    )


def test_officer_decision_requires_a_reason(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())

    response = client.post(
        "/api/agent/officer/applications/1/decision",
        headers={"Authorization": "Bearer signed-token"},
        json={"decision": "approve", "reason": " "},
    )

    assert response.status_code == 400
    assert "reason is required" in response.json["error"]


def test_citizen_token_cannot_read_officer_queue(client, monkeypatch):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: citizen_claims())

    response = client.get(
        "/api/agent/officer/applications",
        headers={"Authorization": "Bearer signed-token"},
    )

    assert response.status_code == 403


def test_officer_can_list_approved_applications_across_portal_blocks(client, monkeypatch):
    submitted_at = datetime.datetime(2026, 10, 20, 9, 30)
    approved_at = datetime.datetime(2026, 10, 21, 14, 15)
    queries = []

    class Cursor:
        def __init__(self):
            self.fetchall_calls = 0

        def execute(self, query, params):
            queries.append((query, params))

        def fetchone(self):
            return (1,)

        def fetchall(self):
            self.fetchall_calls += 1
            if self.fetchall_calls == 1:
                return [(
                    42,
                    "SSP12345678",
                    "Kamla Devi",
                    datetime.date(1956, 4, 12),
                    "Female",
                    "Rajapara Bazaar",
                    "Rajapara",
                    submitted_at,
                    approved_at,
                    "admin",
                )]
            return [(
                42,
                "APPROVED",
                approved_at,
                "officer:admin",
                "Documents verified.",
            )]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(
        agent_api,
        "_authorized",
        lambda role: ((officer_claims(), "admin"), None),
    )
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/approved-applications",
        headers={"Authorization": "******"},
    )

    assert response.status_code == 200
    assert response.json["enabled_blocks"] == portal.BLOCKS
    assert response.json["pagination"] == {
        "limit": 20,
        "offset": 0,
        "total_count": 1,
        "has_more": False,
    }
    assert response.json["applications"] == [{
        "application_id": 42,
        "application_number": "SSP12345678",
        "applicant_name": "Kamla Devi",
        "date_of_birth": "1956-04-12",
        "gender": "Female",
        "village": "Rajapara Bazaar",
        "block": "Rajapara",
        "submitted_at": submitted_at.isoformat(),
        "status": "APPROVED",
        "approved_at": approved_at.isoformat(),
        "approving_officer": "admin",
        "approval_reason": "Documents verified.",
        "decision_history": [{
            "decision": "APPROVED",
            "at": approved_at.isoformat(),
            "officer": "officer:admin",
            "reason": "Documents verified.",
        }],
    }]
    assert "a.status = 'APPROVED'" in queries[1][0]
    assert "ORDER BY a.decided_at DESC NULLS LAST, a.id DESC" in queries[1][0]
    assert queries[1][1] == (portal.BLOCKS, 20, 0)
    assert all(query.lstrip().startswith("SELECT") for query, _ in queries)


def test_approved_application_lookup_filters_and_paginates(client, monkeypatch):
    calls = []

    class Cursor:
        def __init__(self):
            self.fetchall_calls = 0

        def execute(self, query, params):
            calls.append((query, params))

        def fetchone(self):
            return (8,)

        def fetchall(self):
            self.fetchall_calls += 1
            if self.fetchall_calls == 1:
                return [(
                    9,
                    "SSP00000009",
                    "Applicant",
                    datetime.date(1955, 1, 1),
                    "Female",
                    "Village",
                    "Sonari",
                    datetime.datetime(2026, 10, 1, 10),
                    datetime.datetime(2026, 10, 10, 11),
                    "officer-1",
                )]
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(
        agent_api,
        "_authorized",
        lambda role: ((officer_claims(), "officer-1"), None),
    )
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari,Rajapara")
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/approved-applications",
        query_string={
            "limit": "5",
            "offset": "5",
            "submitted_from": "2026-10-01",
            "submitted_to": "2026-10-24",
            "decision_from": "2026-10-02",
            "decision_to": "2026-10-20",
            "block": "Sonari",
        },
        headers={"Authorization": "******"},
    )

    assert response.status_code == 200
    assert response.json["pagination"] == {
        "limit": 5,
        "offset": 5,
        "total_count": 8,
        "has_more": True,
    }
    assert calls[0][1] == (
        ["Sonari"],
        datetime.date(2026, 10, 1),
        datetime.date(2026, 10, 25),
        datetime.date(2026, 10, 2),
        datetime.date(2026, 10, 21),
    )
    assert calls[1][1] == calls[0][1] + (5, 5)


def test_approved_application_lookup_returns_empty_without_fabricating(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            pass

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(
        agent_api,
        "_authorized",
        lambda role: ((officer_claims(), "admin"), None),
    )
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/approved-applications",
        headers={"Authorization": "******"},
    )

    assert response.status_code == 200
    assert response.json["applications"] == []
    assert response.json["pagination"]["total_count"] == 0


@pytest.mark.parametrize(
    ("query", "status", "error_fragment"),
    [
        ({"submitted_from": "not-a-date"}, 400, "YYYY-MM-DD"),
        ({"submitted_to": "2026-1-1"}, 400, "YYYY-MM-DD"),
        (
            {"decision_from": "2026-10-25", "decision_to": "2026-10-24"},
            400,
            "cannot be later",
        ),
        ({"block": "OutsideScope"}, 403, "not agent-enabled"),
        ({"limit": "51"}, 400, "limit must be"),
        ({"offset": "-1"}, 400, "offset must be"),
    ],
)
def test_approved_application_lookup_validates_filters_and_scope(
    client, monkeypatch, query, status, error_fragment
):
    monkeypatch.setattr(
        agent_api,
        "_authorized",
        lambda role: ((officer_claims(), "admin"), None),
    )
    monkeypatch.setenv("MCP_ENABLED_BLOCKS", "Sonari")

    response = client.get(
        "/api/agent/officer/approved-applications",
        query_string=query,
        headers={"Authorization": "******"},
    )

    assert response.status_code == status
    assert error_fragment in response.json["error"]


def test_officer_tools_reject_requests_without_a_token(client):
    paths = [
        ("/api/agent/officer/sla-risk", "GET"),
        ("/api/agent/officer/deemed-approved", "GET"),
        ("/api/agent/officer/approved-applications", "GET"),
        ("/api/agent/officer/search", "GET"),
        ("/api/agent/officer/applicant-history", "GET"),
        ("/api/agent/officer/applications/1/duplicates", "GET"),
        ("/api/agent/officer/applications/1/bank-verification", "GET"),
        ("/api/agent/officer/applications/1/document", "GET"),
        ("/api/agent/officer/applications/1/field-note", "POST"),
        ("/api/agent/officer/applications/batch-decision", "POST"),
        ("/api/agent/officer/applications/1/flag", "POST"),
        ("/api/agent/officer/queue-summary", "GET"),
        ("/api/agent/officer/village-breakdown", "GET"),
    ]

    for path, method in paths:
        response = client.open(path, method=method, json={})
        assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/agent/officer/sla-risk", {"days_threshold": "invalid"}),
        ("/api/agent/officer/deemed-approved", {"days_back": "0"}),
        ("/api/agent/officer/search", {"query": "Kamla", "status": "UNKNOWN"}),
        ("/api/agent/officer/applicant-history", {"mobile": "123"}),
    ],
)
def test_officer_read_tools_validate_query_arguments(client, monkeypatch, path, query):
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())

    response = client.get(
        path,
        query_string=query,
        headers={"Authorization": "Bearer officer-token"},
    )

    assert response.status_code == 400


def test_sla_risk_tool_uses_configured_sla_warning_window(client, monkeypatch):
    now = datetime.datetime(2026, 10, 24, 12, 0)
    submitted_at = now - datetime.timedelta(days=13)
    calls = []

    class Cursor:
        def execute(self, query, params):
            calls.append((query, params))

        def fetchall(self):
            return [(1, "SSP12345678", "Kamla Devi", "9000000001",
                     "Sonari Basti", "Sonari", submitted_at)]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "now_ist", lambda: now)
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/sla-risk",
        headers={"Authorization": "Bearer officer-token"},
    )

    assert response.status_code == 200
    assert response.json["minimum_queue_age_days"] == 12
    assert response.json["applications"][0]["days_in_queue"] == 13
    assert calls[0][1][1] == now - datetime.timedelta(days=12)


def test_search_tool_masks_bank_account_in_results(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            self.query = query
            self.params = params

        def fetchall(self):
            return [(1, "SSP12345678", "Kamla Devi", "9000000001",
                     "Sonari Basti", "Sonari", "PENDING", None, "9012")]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/search",
        query_string={"query": "Kamla", "status": "pending"},
        headers={"Authorization": "Bearer officer-token"},
    )

    assert response.status_code == 200
    assert response.json["applications"][0]["bank_account_last4"] == "9012"
    assert "bank_account" not in response.json["applications"][0]


def test_bank_verification_reports_format_only_and_legacy_prefix(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            pass

        def fetchone(self):
            return ("123456789012", "UTBI0PR2819")

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", Connection)

    response = client.get(
        "/api/agent/officer/applications/1/bank-verification",
        headers={"Authorization": "Bearer officer-token"},
    )

    assert response.status_code == 200
    assert response.json["account_number_valid"] is True
    assert response.json["ifsc_format_valid"] is True
    assert response.json["legacy_merged_bank_prefix"] is True
    assert response.json["registry_check_performed"] is False


def test_batch_decisions_roll_back_if_any_case_is_not_pending(client, monkeypatch):
    class Cursor:
        def execute(self, query, params):
            self.query = query

        def fetchall(self):
            return [(1, "SSP12345678", "9000000001")]

    class Connection:
        rolled_back = False
        committed = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

        def commit(self):
            self.committed = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)

    response = client.post(
        "/api/agent/officer/applications/batch-decision",
        headers={"Authorization": "Bearer officer-token"},
        json={
            "application_ids": [1, 2],
            "decision": "approve",
            "reason": "Verified",
        },
    )

    assert response.status_code == 409
    assert connection.rolled_back is True
    assert connection.committed is False


def test_field_verification_note_adds_audit_without_changing_status(client, monkeypatch):
    audit_inserts = []

    class Cursor:
        last_query = ""
        last_params = ()

        def execute(self, query, params):
            self.last_query = query
            self.last_params = params
            if query.startswith("INSERT INTO audit_log"):
                audit_inserts.append(params)

        def fetchone(self):
            return ("PENDING",) if "SELECT status FROM applications" in self.last_query else None

    class Connection:
        committed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)

    response = client.post(
        "/api/agent/officer/applications/1/field-note",
        headers={"Authorization": "Bearer officer-token"},
        json={
            "note": "Physical verification complete.",
            "verified_by": "Panchayat Secretary",
        },
    )

    assert response.status_code == 200
    assert response.json["action"] == "FIELD_NOTE"
    assert connection.committed is True
    assert "Panchayat Secretary" in audit_inserts[0][3]


def test_flagged_case_is_saved_for_deemed_approval_exclusion(client, monkeypatch):
    audit_inserts = []
    statements = []

    class Cursor:
        last_query = ""
        last_params = ()

        def execute(self, query, params=None):
            self.last_query = query
            self.last_params = params
            statements.append(query)
            if query.startswith("INSERT INTO audit_log"):
                audit_inserts.append(params)

        def fetchone(self):
            if "SELECT status FROM applications" in self.last_query:
                return ("PENDING",)
            if "INSERT INTO application_flags" in self.last_query:
                return (1,)
            return None

    class Connection:
        committed = False
        rolled_back = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.committed = True

        def rollback(self):
            self.rolled_back = True

        def close(self):
            pass

    connection = Connection()
    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", lambda: connection)

    response = client.post(
        "/api/agent/officer/applications/1/flag",
        headers={"Authorization": "Bearer officer-token"},
        json={
            "flag_type": "ADDRESS_UNVERIFIED",
            "reason": "Confirm the address before processing.",
        },
    )

    assert response.status_code == 200
    assert response.json["held_from_deemed_approval"] is True
    assert connection.committed is True
    assert not connection.rolled_back
    assert any(query.startswith("INSERT INTO application_flags") for query in statements)
    assert "ADDRESS_UNVERIFIED" in audit_inserts[0][3]
    assert not any(query.startswith("UPDATE applications") for query in statements)


def test_document_tool_returns_base64_only_for_stored_upload(client, monkeypatch, tmp_path):
    document = tmp_path / "proof.pdf"
    document.write_bytes(b"synthetic-proof")

    class Cursor:
        def execute(self, query, params):
            pass

        def fetchone(self):
            return (str(document),)

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(agent_api, "verify_token", lambda token, audience: officer_claims())
    monkeypatch.setattr(portal, "get_db", Connection)
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))

    response = client.get(
        "/api/agent/officer/applications/1/document",
        headers={"Authorization": "Bearer officer-token"},
    )

    assert response.status_code == 200
    assert response.json["filename"] == "proof.pdf"
    assert response.json["document_type"] == "PDF"
    assert base64.b64decode(response.json["content_base64"]) == b"synthetic-proof"


def test_sms_gateway_is_not_available_without_admin_session(client, monkeypatch):
    def unexpected_request(*args, **kwargs):
        pytest.fail("gateway must not be contacted for unauthenticated requests")

    monkeypatch.setattr(portal.requests, "get", unexpected_request)

    response = client.get("/__gateway/api/messages")

    assert response.status_code == 403


def test_sms_gateway_is_available_to_admin_session(client, monkeypatch):
    class GatewayResponse:
        content = b'{"messages":[]}'
        status_code = 200
        headers = {"Content-Type": "application/json"}

    monkeypatch.setattr(portal, "SMS_GATEWAY_TOKEN", "test-gateway-token-for-unit-tests-123")
    gateway_request = {}

    def get_gateway(*args, **kwargs):
        gateway_request.update(kwargs)
        return GatewayResponse()

    monkeypatch.setattr(portal.requests, "get", get_gateway)
    with client.session_transaction() as flask_session:
        flask_session["admin"] = True

    response = client.get("/__gateway/api/messages")

    assert response.status_code == 200
    assert response.json == {"messages": []}
    assert gateway_request["headers"]["Authorization"] == (
        "Bearer test-gateway-token-for-unit-tests-123"
    )


def test_upload_uses_server_generated_filename(client, monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"

    response = client.post(
        "/upload",
        data={"document": (io.BytesIO(valid_pdf_bytes()), "../../outside.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    uploaded_files = list(tmp_path.iterdir())
    assert len(uploaded_files) == 1
    assert uploaded_files[0].name.endswith(".pdf")
    assert uploaded_files[0].name != "../../outside.pdf"
    assert portal.valid_uploaded_proof(str(uploaded_files[0]))


def test_bank_step_saves_answers_and_continues_to_review_without_proof(client, monkeypatch):
    application_answers = {
        "applicant_name": "Kamla Devi",
        "dob": "12/04/1956",
        "village": "Sonari Basti",
        "block": "Sonari",
        "bank_account": "123456789012",
        "ifsc": "SBIN0003077",
    }
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-1"
        flask_session["form_data"] = {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
        }
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (False, False, False))
    monkeypatch.setattr(portal, "mark_web_draft_reviewed", lambda mobile, draft_id: None)

    response = client.post("/form/3", data={
        "bank_account": application_answers["bank_account"],
        "ifsc": application_answers["ifsc"],
    })

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/declaration")
    review = client.get("/declaration")
    assert review.status_code == 200
    assert b"Your proof document is missing. Please upload it now to continue." in review.data
    assert b"Kamla Devi" in review.data
    assert b"********9012" in review.data
    assert b"123456789012" not in review.data


def test_failed_proof_upload_preserves_answers_for_retry(client, monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    answers = {"applicant_name": "Kamla Devi", "village": "Sonari Basti"}
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["form_data"] = answers.copy()

    response = client.post(
        "/upload",
        data={"document": (io.BytesIO(b"not-a-pdf"), "proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"file contents do not match" in response.data
    assert b"Kamla Devi" in response.data
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"] == answers
        assert "doc_path" not in flask_session


def test_storage_failure_keeps_answers_and_allows_retry(client, monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path / "unavailable"))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["form_data"] = {"applicant_name": "Kamla Devi"}

    def fail_makedirs(*args, **kwargs):
        raise PermissionError("storage unavailable")

    monkeypatch.setattr(portal.os, "makedirs", fail_makedirs)
    response = client.post(
        "/upload",
        data={"document": (io.BytesIO(valid_pdf_bytes()), "proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert b"could not be saved" in response.data
    assert b"Kamla Devi" in response.data
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"] == {"applicant_name": "Kamla Devi"}
        assert "doc_path" not in flask_session


def test_successful_proof_upload_returns_to_review_and_is_not_requested_again(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-2"
        flask_session["form_data"] = {"applicant_name": "Kamla Devi"}
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (False, False, False))
    monkeypatch.setattr(portal, "mark_web_draft_reviewed", lambda mobile, draft_id: None)

    response = client.post(
        "/upload",
        data={"document": (io.BytesIO(valid_pdf_bytes()), "proof.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/declaration")
    with client.session_transaction() as flask_session:
        uploaded_path = flask_session["doc_path"]
        assert flask_session["form_data"] == {"applicant_name": "Kamla Devi"}
    assert portal.valid_uploaded_proof(uploaded_path)

    review = client.get("/declaration")
    assert review.status_code == 200
    assert b"Choose age-proof file" not in review.data
    assert b"Upload your proof document" not in review.data
    assert b"proof.pdf" in review.data


def test_uploaded_proof_type_and_size_are_validated(client, monkeypatch, tmp_path):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["form_data"] = {"applicant_name": "Kamla Devi"}

    wrong_type = client.post(
        "/upload",
        data={"document": (io.BytesIO(b"not an image"), "proof.png")},
        content_type="multipart/form-data",
    )
    assert b"file contents do not match" in wrong_type.data

    too_large = client.post(
        "/upload",
        data={"document": (io.BytesIO(b"%PDF-" + b"x" * (5 * 1024 * 1024)), "large.pdf")},
        content_type="multipart/form-data",
    )
    assert b"larger than 5 MB" in too_large.data
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"] == {"applicant_name": "Kamla Devi"}
        assert "doc_path" not in flask_session


def test_final_submission_does_not_proceed_with_missing_proof(client, monkeypatch):
    def unexpected_db_connection():
        pytest.fail("submission must be blocked before accessing the database")

    monkeypatch.setattr(portal, "get_db", unexpected_db_connection)
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-3"
        flask_session["form_data"] = {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": "123456789012",
            "ifsc": "SBIN0003077",
        }
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (False, False, False))
    monkeypatch.setattr(portal, "mark_web_draft_reviewed", lambda mobile, draft_id: None)

    response = client.post("/declaration")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/declaration")
    review = client.get("/declaration")
    assert b"Your proof document is missing" in review.data
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"]["applicant_name"] == "Kamla Devi"


def test_direct_form_post_after_review_cannot_bypass_one_time_edit(client, monkeypatch):
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (True, True, False))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-reviewed"
        flask_session["form_data"] = {"applicant_name": "Original Name"}

    response = client.post(
        "/form/1",
        data={"applicant_name": "Unauthorized Change"},
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/declaration")
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"]["applicant_name"] == "Original Name"


def test_direct_submission_before_review_redirects_to_review(client, monkeypatch):
    reviewed = {"value": False}
    monkeypatch.setattr(
        portal,
        "web_draft_state",
        lambda mobile, draft_id: (reviewed["value"], False, False),
    )
    monkeypatch.setattr(
        portal,
        "mark_web_draft_reviewed",
        lambda mobile, draft_id: reviewed.update(value=True),
    )
    monkeypatch.setattr(
        portal,
        "get_db",
        lambda: pytest.fail("application must not submit before review"),
    )
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-before-review"
        flask_session["form_data"] = {"applicant_name": "Applicant"}

    response = client.post("/declaration")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/declaration")
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"]["applicant_name"] == "Applicant"


@pytest.mark.parametrize(
    ("bank_account", "expected_error"),
    [
        ("12345678", "at least 9 digits"),
        ("1234567890123456789", "no more than 18 digits"),
        ("12345A789", "digits only"),
        ("123 56789", "at least 9 digits"),
        ("123-56789", "digits only"),
    ],
)
def test_bank_account_rejects_invalid_characters_and_lengths(bank_account, expected_error):
    errors, _ = portal.validate_application(
        {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": bank_account,
        },
        "valid-proof",
    )

    assert any(expected_error in error for error in errors)


@pytest.mark.parametrize("bank_account", ["012345678", "012345678901234567"])
def test_bank_account_preserves_leading_zero_and_accepts_9_to_18_digits(bank_account):
    errors, cleaned = portal.validate_application(
        {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": bank_account,
        },
        "valid-proof",
    )

    assert errors == []
    assert not any("bank account" in error for error in errors)
    assert cleaned["bank_account"] == bank_account


def test_invalid_block_is_rejected_by_backend_validation():
    errors, _ = portal.validate_application(
        {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Not a portal block",
            "bank_account": "012345678",
        },
        "valid-proof",
    )

    assert any("block must be one of" in error for error in errors)


def test_edit_session_saves_once_and_disables_future_editing(
    client, monkeypatch, tmp_path
):
    monkeypatch.setattr(portal, "UPLOAD_DIR", str(tmp_path))
    proof_path = tmp_path / "proof.pdf"
    proof_path.write_bytes(valid_pdf_bytes())
    state = {"used": False}
    monkeypatch.setattr(
        portal,
        "web_draft_state",
        lambda mobile, draft_id: (True, state["used"], False),
    )
    monkeypatch.setattr(portal, "mark_web_draft_reviewed", lambda mobile, draft_id: None)

    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-edit"
        flask_session["form_data"] = {
            "applicant_name": "Kamla Devi",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": "012345678",
            "ifsc": "SBIN0003077",
        }
        flask_session["doc_path"] = str(proof_path)
        flask_session["doc_name"] = "proof.pdf"

    monkeypatch.setattr(
        portal,
        "save_web_draft_edit",
        lambda mobile, draft_id: state.update(used=True) is None,
    )
    edit = client.post("/application/edit")
    assert edit.status_code == 302
    assert edit.headers["Location"].endswith("/form/1")

    changed = client.post("/form/1", data={
        "applicant_name": "Kamla Devi Updated",
        "dob": "12/04/1956",
        "gender": "Female",
        "marital_status": "Married",
        "husband_name": "",
    })
    assert changed.headers["Location"].endswith("/form/2")
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"]["applicant_name"] == "Kamla Devi"
        assert flask_session["edit_form_data"]["applicant_name"] == "Kamla Devi Updated"

    # Save candidate edits directly at final review while preserving the uploaded file.
    with client.session_transaction() as flask_session:
        flask_session["edit_form_data"].update({
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": "012345678",
            "ifsc": "SBIN0003077",
        })
        flask_session["editing_application"] = True

    saved = client.post("/declaration", data={"action": "save_edit"})
    assert saved.status_code == 302
    review = client.get("/declaration")
    assert b"Kamla Devi Updated" in review.data
    assert b"one-time editing opportunity has been used" in review.data
    assert b"Edit Application" not in review.data
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"]["applicant_name"] == "Kamla Devi Updated"
        assert flask_session["doc_path"] == str(proof_path)


def test_failed_edit_save_preserves_original_answers_and_edit_candidate(client, monkeypatch):
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (True, False, False))
    monkeypatch.setattr(portal, "save_web_draft_edit", lambda mobile, draft_id: False)
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-failed-save"
        flask_session["form_data"] = {"applicant_name": "Original Name"}
        flask_session["edit_form_data"] = {
            "applicant_name": "Corrected Name",
            "dob": "12/04/1956",
            "village": "Sonari Basti",
            "block": "Sonari",
            "bank_account": "012345678",
        }
        flask_session["editing_application"] = True

    response = client.post("/declaration", data={"action": "save_edit"})

    assert response.status_code == 302
    with client.session_transaction() as flask_session:
        assert flask_session["form_data"] == {"applicant_name": "Original Name"}
        assert flask_session["edit_form_data"]["applicant_name"] == "Corrected Name"
        assert flask_session["editing_application"] is True


def test_used_edit_opportunity_cannot_be_started_again(client, monkeypatch):
    monkeypatch.setattr(portal, "web_draft_state", lambda mobile, draft_id: (True, True, False))
    with client.session_transaction() as flask_session:
        flask_session["verified_mobile"] = "9000000001"
        flask_session["web_draft_id"] = "draft-used"
        flask_session["form_data"] = {"applicant_name": "Original Name"}

    response = client.post("/application/edit")

    assert response.status_code == 302
    with client.session_transaction() as flask_session:
        assert "editing_application" not in flask_session


def test_authorization_metadata_matches_mcp_resource_discovery(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json

    assert metadata["issuer"] == oauth.ISSUER
    assert metadata["authorization_endpoint"] == oauth.ISSUER_BASE + "/oauth/authorize"
    assert metadata["token_endpoint"] == oauth.ISSUER_BASE + "/oauth/token"


def test_metadata_fetch_pins_public_ip_and_parses_document(monkeypatch):
    class MetadataResponse:
        status = 200

        def getheader(self, name):
            return "40"

        def read(self, limit):
            return b'{"client_id":"https://client.example/metadata.json"}'

    class MetadataConnection:
        def __init__(self, host, port, address):
            assert host == "client.example"
            assert port == 443
            assert address == "93.184.216.34"

        def request(self, method, path, headers):
            assert method == "GET"
            assert path == "/metadata.json"
            assert headers["Host"] == "client.example"

        def getresponse(self):
            return MetadataResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        oauth.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(oauth, "_PinnedHTTPSConnection", MetadataConnection)

    document = oauth._fetch_client_metadata("https://client.example/metadata.json")

    assert document["client_id"] == "https://client.example/metadata.json"


def test_metadata_fetch_rejects_private_addresses(monkeypatch):
    def unexpected_connection(*args, **kwargs):
        pytest.fail("private destinations must not be contacted")

    monkeypatch.setattr(
        oauth.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))],
    )
    monkeypatch.setattr(oauth, "_PinnedHTTPSConnection", unexpected_connection)

    assert oauth._fetch_client_metadata("https://client.example/metadata.json") is None


def test_metadata_fetch_does_not_follow_redirects(monkeypatch):
    class RedirectResponse:
        status = 302

        def getheader(self, name):
            return "https://127.0.0.1/metadata.json"

    class RedirectConnection:
        def __init__(self, host, port, address):
            pass

        def request(self, method, path, headers):
            pass

        def getresponse(self):
            return RedirectResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        oauth.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(None, None, None, None, ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(oauth, "_PinnedHTTPSConnection", RedirectConnection)

    assert oauth._fetch_client_metadata("https://client.example/metadata.json") is None


@pytest.mark.parametrize(
    "issuer",
    ["http://localhost:8000", "http://localhost:8000/"],
)
def test_portal_verifies_current_and_legacy_issuer(monkeypatch, issuer):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(oauth, "signing_key", lambda: ("test", private_key))
    token = jwt.encode(
        {
            "iss": issuer,
            "sub": "citizen:9000000001",
            "aud": "http://localhost:8001/mcp",
            "scope": "citizen",
            "role": "citizen",
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
    )

    claims = oauth.verify_token(token, "http://localhost:8001/mcp")

    assert claims["iss"] == issuer
