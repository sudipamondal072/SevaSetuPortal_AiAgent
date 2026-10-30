import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from flask import Blueprint, g, has_request_context, jsonify, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
import io
from werkzeug.utils import secure_filename

import app as portal
from oauth import SESSION, mint_access_token, verify_token

agent_api = Blueprint("agent_api", __name__)
MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
try:
    DRAFT_RETENTION_DAYS = int(os.getenv("CITIZEN_DRAFT_RETENTION_DAYS", "30"))
except ValueError as exc:
    raise RuntimeError("CITIZEN_DRAFT_RETENTION_DAYS must be a positive integer.") from exc
if DRAFT_RETENTION_DAYS < 1:
    raise RuntimeError("CITIZEN_DRAFT_RETENTION_DAYS must be a positive integer.")
DRAFT_FIELDS = (
    "applicant_name", "dob", "village", "block", "bank_account", "ifsc",
    "gender", "marital_status", "husband_name",
)
_CITIZEN_DRAFT_SCHEMA_READY = False
_CITIZEN_DRAFT_SCHEMA_LOCK = threading.Lock()
_CITIZEN_SESSION_SCHEMA_READY = False
_CITIZEN_SESSION_SCHEMA_LOCK = threading.Lock()
_LEGACY_BLOCK_CONFIGURATION_WARNED = False


class _TimedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def _timed(self, operation, function, *args, **kwargs):
        started = time.perf_counter()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if has_request_context():
                g.agent_db_ms = getattr(g, "agent_db_ms", 0.0) + elapsed_ms
                if operation == "execute":
                    g.agent_db_queries = getattr(g, "agent_db_queries", 0) + 1
                route = request.url_rule.rule if request.url_rule else "unmatched"
            else:
                route = "outside_request"
            portal.app.logger.info(
                "agent_db_step route=%s operation=%s elapsed_ms=%.2f",
                route,
                operation,
                elapsed_ms,
            )

    def execute(self, *args, **kwargs):
        return self._timed("execute", self._cursor.execute, *args, **kwargs)

    def fetchone(self, *args, **kwargs):
        return self._timed("fetchone", self._cursor.fetchone, *args, **kwargs)

    def fetchall(self, *args, **kwargs):
        return self._timed("fetchall", self._cursor.fetchall, *args, **kwargs)

    def fetchmany(self, *args, **kwargs):
        return self._timed("fetchmany", self._cursor.fetchmany, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


@agent_api.before_app_request
def _start_agent_request_timer():
    if request.path.startswith("/api/agent/"):
        g.agent_request_started = time.perf_counter()
        g.agent_db_ms = 0.0
        g.agent_db_queries = 0


@agent_api.after_app_request
def _log_agent_request_timing(response):
    started = getattr(g, "agent_request_started", None)
    if started is not None:
        route = request.url_rule.rule if request.url_rule else "unmatched"
        portal.app.logger.info(
            "agent_request route=%s method=%s status=%s total_ms=%.2f "
            "db_queries=%s db_ms=%.2f",
            route,
            request.method,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            getattr(g, "agent_db_queries", 0),
            getattr(g, "agent_db_ms", 0.0),
        )
    return response


@contextmanager
def _timed_step(name):
    started = time.perf_counter()
    try:
        yield
    finally:
        portal.app.logger.info(
            "agent_step step=%s elapsed_ms=%.2f",
            name,
            (time.perf_counter() - started) * 1000,
        )


def _timed_operation(name, function, *args, **kwargs):
    with _timed_step(name):
        return function(*args, **kwargs)


def _stored_document_matches_upload(path, contents):
    resolved_path = os.path.realpath(path)
    upload_root = os.path.realpath(portal.UPLOAD_DIR)
    try:
        if (
            os.path.commonpath((upload_root, resolved_path)) != upload_root
            or not os.path.basename(resolved_path).startswith("citizen_draft_")
            or not os.path.isfile(resolved_path)
            or os.path.getsize(resolved_path) != len(contents)
        ):
            return False
        with open(resolved_path, "rb") as stored_file:
            return stored_file.read(MAX_DOCUMENT_BYTES + 1) == contents
    except (OSError, ValueError):
        return False


@agent_api.errorhandler(RequestEntityTooLarge)
def handle_upload_too_large(_error):
    return jsonify(error="Request exceeds the allowed upload size."), 413


def _ensure_citizen_drafts_table(cur):
    global _CITIZEN_DRAFT_SCHEMA_READY
    if _CITIZEN_DRAFT_SCHEMA_READY:
        return
    with _CITIZEN_DRAFT_SCHEMA_LOCK:
        if _CITIZEN_DRAFT_SCHEMA_READY:
            return
        cur.execute(
            """CREATE TABLE IF NOT EXISTS citizen_application_drafts (
                   mobile VARCHAR(15) PRIMARY KEY,
                   draft_ref VARCHAR(64) NOT NULL UNIQUE,
                   draft_data JSONB NOT NULL DEFAULT '{}'::jsonb,
                   doc_path VARCHAR(500),
                   doc_id VARCHAR(64),
                   doc_filename VARCHAR(255),
                   document_status VARCHAR(20) NOT NULL DEFAULT 'PENDING_UPLOAD',
                   document_message VARCHAR(500),
                   completed BOOLEAN NOT NULL DEFAULT FALSE,
                   created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
               )"""
        )
        cur.execute(
            "ALTER TABLE citizen_application_drafts "
            "ADD COLUMN IF NOT EXISTS doc_id VARCHAR(64)"
        )
        cur.execute(
            "ALTER TABLE citizen_application_drafts "
            "ALTER COLUMN document_status SET DEFAULT 'PENDING_UPLOAD'"
        )
        cur.execute(
            "UPDATE citizen_application_drafts SET document_status = 'PENDING_UPLOAD' "
            "WHERE document_status = 'MISSING' AND doc_path IS NULL"
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS citizen_draft_reviews (
                   id SERIAL PRIMARY KEY,
                   draft_ref VARCHAR(64) NOT NULL,
                   officer VARCHAR(50) NOT NULL,
                   decision VARCHAR(20) NOT NULL,
                   reason VARCHAR(500) NOT NULL,
                   reviewed_at TIMESTAMP NOT NULL
               )"""
        )
        connection = getattr(cur, "connection", None)
        if connection is not None:
            connection.commit()
            _CITIZEN_DRAFT_SCHEMA_READY = True


def _draft_data(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _mask_bank_account(value):
    return portal.mask_bank_account(value)


def _remove_expired_draft_file(path):
    if not path:
        return
    upload_root = os.path.realpath(portal.UPLOAD_DIR)
    resolved_path = os.path.realpath(path)
    try:
        safe = (
            os.path.commonpath((upload_root, resolved_path)) == upload_root
            and os.path.basename(resolved_path).startswith("citizen_draft_")
        )
    except ValueError:
        safe = False
    if safe and os.path.isfile(resolved_path):
        try:
            os.remove(resolved_path)
        except OSError:
            portal.app.logger.exception("expired citizen draft file cleanup failed")


def _draft_response(row):
    data = _draft_data(row[1])
    data["bank_account"] = _mask_bank_account(data.get("bank_account"))
    document_status = (
        "PENDING_UPLOAD" if row[4] == "MISSING" else row[4]
    )
    return {
        "draft_ref": row[0],
        "details": data,
        "document_status": document_status,
        "document_message": portal.mask_sensitive_numbers(row[5]),
        "document_attached": bool(row[2]),
        "draft_status": "COMPLETED" if row[6] else "DRAFT",
        "next_step": (
            "review_and_confirm" if document_status == "VALID"
            else "officer_document_review" if document_status == "REVIEW_REQUIRED"
            else "validate_age_proof" if document_status == "PENDING_VALIDATION"
            else "attach_age_proof"
        ),
    }


def _read_owned_draft(cur, mobile, draft_ref, *, for_update=False):
    suffix = " FOR UPDATE" if for_update else ""
    cur.execute(
        "SELECT draft_ref, draft_data, doc_path, doc_filename, document_status, "
        "document_message, completed, doc_id, "
        "GREATEST(0, EXTRACT(EPOCH FROM (CURRENT_TIMESTAMP - created_at)) * 1000) "
        "FROM citizen_application_drafts "
        "WHERE mobile = %s AND draft_ref = %s "
        "AND updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')" + suffix,
        (mobile, draft_ref, DRAFT_RETENTION_DAYS),
    )
    return cur.fetchone()


def _clean_draft_updates(data):
    updates = {}
    for field in DRAFT_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, str):
            return None, "%s must be text." % field
        if field == "bank_account":
            value = re.sub(r"\s+", "", value)
            if value and not re.fullmatch(r"[0-9]+", value):
                return None, "Bank account number must contain digits only."
            if value and len(value) not in portal.BANK_ACCOUNT_LENGTHS:
                return None, (
                    "Bank account number must have one of these accepted lengths: %s digits."
                    % ", ".join(str(length) for length in portal.BANK_ACCOUNT_LENGTHS)
                )
        updates[field] = value.strip()
    return updates, None


def enabled_blocks():
    global _LEGACY_BLOCK_CONFIGURATION_WARNED
    if os.getenv("MCP_ENABLED_BLOCKS") is not None:
        with _CITIZEN_DRAFT_SCHEMA_LOCK:
            if not _LEGACY_BLOCK_CONFIGURATION_WARNED:
                portal.app.logger.warning(
                    "MCP_ENABLED_BLOCKS is deprecated and ignored; MCP operations "
                    "use all blocks in the portal catalog."
                )
                _LEGACY_BLOCK_CONFIGURATION_WARNED = True
    return list(dict.fromkeys(portal.BLOCKS))


def _authorized(role):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None, (jsonify(error="A Sewa Setu access token is required."), 401)

    resource = os.getenv(
        "CITIZEN_RESOURCE_URL" if role == "citizen" else "OFFICER_RESOURCE_URL",
        "http://localhost:8001/mcp" if role == "citizen"
        else "http://localhost:8002/mcp",
    ).rstrip("/")
    claims = verify_token(token, resource)
    if not claims:
        return None, (jsonify(error="The access token is invalid or expired."), 401)

    scopes = claims.get("scope", "")
    scopes = scopes.split() if isinstance(scopes, str) else scopes
    if claims.get("role") != role or role not in scopes:
        return None, (jsonify(error="This access token cannot perform this action."), 403)

    subject = claims.get("sub", "")
    prefix = "citizen:" if role == "citizen" else "officer:"
    identity = subject.removeprefix(prefix) if subject.startswith(prefix) else ""
    if not identity:
        return None, (jsonify(error="The access token has an invalid subject."), 403)
    if role == "citizen" and not re.fullmatch(r"\d{10}", identity):
        return None, (jsonify(error="The citizen identity is invalid."), 403)
    return (claims, identity), None


def _json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, (jsonify(error="A JSON object is required."), 400)
    return data, None


def _ensure_citizen_session_schema(cur):
    global _CITIZEN_SESSION_SCHEMA_READY
    if _CITIZEN_SESSION_SCHEMA_READY:
        return
    with _CITIZEN_SESSION_SCHEMA_LOCK:
        if _CITIZEN_SESSION_SCHEMA_READY:
            return
        cur.execute(
            """CREATE TABLE IF NOT EXISTS citizen_mcp_otp_challenges (
                   id SERIAL PRIMARY KEY,
                   mobile VARCHAR(15) NOT NULL,
                   code_hash VARCHAR(64) NOT NULL,
                   created_at TIMESTAMP NOT NULL,
                   consumed_at TIMESTAMP,
                   attempts SMALLINT NOT NULL DEFAULT 0
               )"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_citizen_mcp_otp_mobile "
            "ON citizen_mcp_otp_challenges(mobile, created_at DESC)"
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS citizen_mcp_rate_limits (
                   bucket_key VARCHAR(64) PRIMARY KEY,
                   window_started_at TIMESTAMP NOT NULL,
                   request_count INTEGER NOT NULL
               )"""
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_citizen_mcp_rate_limit_window "
            "ON citizen_mcp_rate_limits(window_started_at)"
        )
        connection = getattr(cur, "connection", None)
        if connection is not None:
            connection.commit()
            _CITIZEN_SESSION_SCHEMA_READY = True


def _rate_limit_key(namespace, value):
    return hmac.new(
        portal.app.secret_key.encode("utf-8"),
        (namespace + ":" + value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _increment_citizen_rate_limit(cur, key, limit, window_seconds, now):
    cur.execute(
        """INSERT INTO citizen_mcp_rate_limits AS current_limit
               (bucket_key, window_started_at, request_count)
           VALUES (%s, %s, 1)
           ON CONFLICT (bucket_key) DO UPDATE SET
               window_started_at = CASE
                   WHEN current_limit.window_started_at <=
                        EXCLUDED.window_started_at - (%s * INTERVAL '1 second')
                   THEN EXCLUDED.window_started_at
                   ELSE current_limit.window_started_at
               END,
               request_count = CASE
                   WHEN current_limit.window_started_at <=
                        EXCLUDED.window_started_at - (%s * INTERVAL '1 second')
                   THEN 1
                   ELSE current_limit.request_count + 1
               END
           RETURNING request_count""",
        (key, now, window_seconds, window_seconds),
    )
    return cur.fetchone()[0] <= limit


def _citizen_otp_rate_limited(cur, mobile, source_ip, action, now):
    if action == "request":
        buckets = (
            ("citizen-otp-request-mobile-15m", mobile, 3, 900),
            ("citizen-otp-request-mobile-hour", mobile, 10, 3600),
            ("citizen-otp-request-ip-hour", source_ip, 1000, 3600),
        )
    else:
        buckets = (
            ("citizen-otp-verify-mobile-15m", mobile, 5, 900),
            ("citizen-otp-verify-ip-hour", source_ip, 1000, 3600),
        )
    return any(
        not _increment_citizen_rate_limit(
            cur,
            _rate_limit_key(namespace, value),
            limit,
            window_seconds,
            now,
        )
        for namespace, value, limit, window_seconds in buckets
    )


def _citizen_otp_code_hash(mobile, code):
    return hmac.new(
        portal.app.secret_key.encode("utf-8"),
        (mobile + ":" + code).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@agent_api.post("/api/agent/citizen/session/otp/request")
def request_citizen_session_otp():
    data, error = _json_object()
    if error:
        return error
    mobile = str(data.get("mobile", "")).strip()
    if not re.fullmatch(r"\d{10}", mobile):
        return jsonify(error="Please provide a valid 10-digit mobile number."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_session_schema(cur)
        now = datetime.now()
        if _citizen_otp_rate_limited(
            cur, mobile, request.remote_addr or "unknown", "request", now
        ):
            conn.commit()
            return jsonify(error="OTP request limit reached. Try again later."), 429, {
                "Retry-After": "900"
            }

        cur.execute(
            "DELETE FROM citizen_mcp_otp_challenges "
            "WHERE created_at < %s - INTERVAL '1 day'",
            (now,),
        )
        cur.execute(
            "DELETE FROM citizen_mcp_rate_limits "
            "WHERE window_started_at < %s - INTERVAL '1 day'",
            (now,),
        )
        code = "%06d" % secrets.randbelow(1000000)
        cur.execute(
            "UPDATE citizen_mcp_otp_challenges SET consumed_at = %s "
            "WHERE mobile = %s AND consumed_at IS NULL",
            (now, mobile),
        )
        cur.execute(
            "INSERT INTO citizen_mcp_otp_challenges "
            "(mobile, code_hash, created_at) VALUES (%s, %s, %s)",
            (mobile, _citizen_otp_code_hash(mobile, code), now),
        )
        conn.commit()
    finally:
        conn.close()

    if not portal.send_sms(
        mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code
    ):
        conn = portal.get_db()
        try:
            cur = _TimedCursor(conn.cursor())
            cur.execute(
                "UPDATE citizen_mcp_otp_challenges SET consumed_at = %s "
                "WHERE mobile = %s AND created_at = %s AND consumed_at IS NULL",
                (datetime.now(), mobile, now),
            )
            conn.commit()
        finally:
            conn.close()
        return jsonify(error="The OTP could not be sent. Please try again later."), 502
    return jsonify(status="otp_sent", expires_in=portal.OTP_VALIDITY_SECONDS)


@agent_api.post("/api/agent/citizen/session/otp/verify")
def verify_citizen_session_otp():
    data, error = _json_object()
    if error:
        return error
    mobile = str(data.get("mobile", "")).strip()
    code = str(data.get("otp", "")).strip()
    if not re.fullmatch(r"\d{10}", mobile) or not re.fullmatch(r"\d{6}", code):
        return jsonify(error="A valid mobile number and six-digit OTP are required."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_session_schema(cur)
        now = datetime.now()
        if _citizen_otp_rate_limited(
            cur, mobile, request.remote_addr or "unknown", "verify", now
        ):
            conn.commit()
            return jsonify(error="OTP verification limit reached. Try again later."), 429, {
                "Retry-After": "900"
            }

        cur.execute(
            "SELECT id, code_hash, created_at, attempts "
            "FROM citizen_mcp_otp_challenges "
            "WHERE mobile = %s AND consumed_at IS NULL "
            "ORDER BY id DESC LIMIT 1 FOR UPDATE",
            (mobile,),
        )
        challenge = cur.fetchone()
        if not challenge:
            conn.commit()
            return jsonify(error="Invalid or expired OTP."), 401

        challenge_id, expected_hash, created_at, attempts = challenge
        age_seconds = (now - created_at).total_seconds()
        if age_seconds < 0 or age_seconds > portal.OTP_VALIDITY_SECONDS:
            cur.execute(
                "UPDATE citizen_mcp_otp_challenges SET consumed_at = %s "
                "WHERE id = %s AND consumed_at IS NULL",
                (now, challenge_id),
            )
            conn.commit()
            return jsonify(error="Invalid or expired OTP."), 401

        supplied_hash = _citizen_otp_code_hash(mobile, code)
        if attempts >= 5 or not hmac.compare_digest(expected_hash, supplied_hash):
            cur.execute(
                "UPDATE citizen_mcp_otp_challenges "
                "SET attempts = attempts + 1, "
                "consumed_at = CASE WHEN attempts + 1 >= 5 THEN %s ELSE consumed_at END "
                "WHERE id = %s AND consumed_at IS NULL",
                (now, challenge_id),
            )
            conn.commit()
            return jsonify(error="Invalid or expired OTP."), 401

        cur.execute(
            "UPDATE citizen_mcp_otp_challenges SET consumed_at = %s "
            "WHERE id = %s AND consumed_at IS NULL",
            (now, challenge_id),
        )
        conn.commit()
    finally:
        conn.close()

    session_ends = now + timedelta(seconds=SESSION["citizen"]["session"])
    session_token, claims = mint_access_token(
        "citizen:" + mobile,
        "citizen",
        "citizen",
        os.getenv("CITIZEN_RESOURCE_URL", "http://localhost:8001/mcp").rstrip("/"),
        "citizen-mcp-otp",
        ttl=SESSION["citizen"]["session"],
        session_ends=session_ends,
    )
    response = jsonify({
        "session_token": session_token,
        "token_type": "Bearer",
        "expires_in": claims["exp"] - int(time.time()),
        "session_expires_in": SESSION["citizen"]["session"],
    })
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _format_date(value):
    return value.isoformat() if value else None


def _officer_scope():
    auth, error = _authorized("officer")
    if error:
        return None, None, error
    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return None, None, (jsonify(error=str(exc)), 500)
    return auth[1], blocks, None


def _selected_blocks(block, blocks):
    if block and block not in blocks:
        return None, (jsonify(error="The requested block is not agent-enabled."), 400)
    return ([block] if block else blocks), None


def _query_integer(name, default, minimum, maximum):
    try:
        value = int(request.args.get(name, str(default)))
    except (TypeError, ValueError):
        return None, (jsonify(error="%s must be an integer between %d and %d."
                              % (name, minimum, maximum)), 400)
    if not minimum <= value <= maximum:
        return None, (jsonify(error="%s must be an integer between %d and %d."
                              % (name, minimum, maximum)), 400)
    return value, None


@agent_api.get("/api/agent/scheme")
def scheme_information():
    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500
    portal_url = os.environ.get("PUBLIC_BASE_URL", request.url_root).rstrip("/")
    return jsonify({
        "scheme_name": "Old Age Pension",
        "application_form_url": portal_url + "/apply",
        "monthly_amount_inr": 250,
        "minimum_age": portal.MIN_AGE,
        "residency": "Resident of Purvanchal",
        "enabled_blocks": blocks,
        "application_window_closes_at": _format_date(portal.SCHEME_DEADLINE),
        "requirements": [
            "Applicant is at least 60 years old",
            "Required personal and address details",
            "Bank account number must be digits only and use an accepted configured length",
            "Age-proof document in PDF, JPG or PNG format, up to 5 MB",
            "Age-proof file checks plus authorized officer review are required",
            "Applicant's declaration and final summary confirmation",
            "The verified mobile number may be reused only when all previous applications are REJECTED or WITHDRAWN",
        ],
        "bank_account_accepted_lengths": list(portal.BANK_ACCOUNT_LENGTHS),
        "village_mapping_policy": (
            "Village names are collected as provided; an unavailable mapping does not "
            "reject an application or establish block membership."
        ),
        "important_note": (
            "The portal validates and records every application. Scheme information "
            "is informational and is not a guarantee of eligibility or approval."
        ),
    })


@agent_api.get("/api/agent/citizen/application")
def citizen_application_status():
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no, status, block, submitted_at, decided_at, "
            "decided_by FROM applications WHERE mobile = %s "
            "ORDER BY CASE WHEN status = 'WITHDRAWN' THEN 1 ELSE 0 END, "
            "submitted_at DESC NULLS LAST LIMIT 1",
            (mobile,),
        )
        row = cur.fetchone()
        if not row or row[2] == "WITHDRAWN":
            _ensure_citizen_drafts_table(cur)
            cur.execute(
                "SELECT draft_ref, draft_data, doc_path, doc_filename, document_status, "
                "document_message, completed FROM citizen_application_drafts "
                "WHERE mobile = %s AND updated_at >= "
                "CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') AND completed = FALSE",
                (mobile, DRAFT_RETENTION_DAYS),
            )
            draft = cur.fetchone()
            if draft:
                conn.commit()
                response = _draft_response(draft)
                if row:
                    response["previous_application"] = {
                        "application_number": row[1],
                        "status": row[2],
                    }
                return jsonify(response)
            if row:
                conn.commit()
                return jsonify({
                    "application_id": row[0],
                    "application_number": row[1],
                    "status": row[2],
                    "block": row[3],
                    "submitted_at": _format_date(row[4]),
                    "decided_at": _format_date(row[5]),
                    "decided_by": row[6],
                    "can_apply_again": True,
                    "message": (
                        "This application was withdrawn. You may start a new "
                        "application using this verified mobile number."
                    ),
                })
            conn.commit()
            return jsonify(error="No application was found for this mobile number."), 404
        cur.execute(
            "SELECT note FROM audit_log WHERE application_id = %s "
            "AND action IN ('APPROVED', 'REJECTED', 'DEEMED_APPROVED') "
            "ORDER BY at DESC LIMIT 1",
            (row[0],),
        )
        reason_row = cur.fetchone()
        return jsonify({
            "application_id": row[0],
            "application_number": row[1],
            "status": row[2],
            "block": row[3],
            "submitted_at": _format_date(row[4]),
            "decided_at": _format_date(row[5]),
            "decided_by": row[6],
            "can_apply_again": False,
            "decision_note": (
                portal.mask_sensitive_numbers(reason_row[0]) if reason_row else None
            ),
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/citizen/application/slip")
def citizen_application_slip():
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no FROM applications WHERE mobile = %s "
            "ORDER BY submitted_at DESC NULLS LAST LIMIT 1",
            (mobile,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="No application was found for this mobile number."), 404
        slip = portal.generate_acknowledgment(cur, row[0])
        return send_file(
            io.BytesIO(slip),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="%s.pdf" % row[1],
        )
    finally:
        conn.close()


@agent_api.post("/api/agent/citizen/applications/<int:application_id>/withdraw")
def citizen_withdraw_application(application_id):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    data, error = _json_object()
    if error:
        return error
    if data.get("confirm") is not True:
        return jsonify(
            error="Explicit confirmation is required to withdraw this application."
        ), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
            "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
            "AND status = 'PENDING' RETURNING application_no",
            (portal.now_ist(), application_id, mobile),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify(
                error="Application was not found for this account or is not pending."
            ), 409
        portal.write_audit(
            cur,
            application_id,
            "WITHDRAW",
            "applicant:%s" % mobile,
        )
        conn.commit()
        return jsonify({
            "application_id": application_id,
            "application_number": row[0],
            "status": "WITHDRAWN",
            "message": "The pending application has been withdrawn.",
        })
    finally:
        conn.close()


@agent_api.post("/api/agent/citizen/application-drafts")
def create_or_resume_citizen_draft():
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    data, error = _json_object()
    if error:
        return error
    updates, error_message = _clean_draft_updates(data)
    if error_message:
        return jsonify(error=error_message), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        existing_application = portal.active_application(cur, mobile)
        if existing_application:
            conn.rollback()
            return jsonify({
                "application_exists": True,
                "application_number": existing_application[1],
                "status": existing_application[2],
                "action": "existing_application",
                "message": (
                    "An application already exists; no new draft or application "
                    "was created."
                ),
            })

        cur.execute(
            "DELETE FROM citizen_application_drafts WHERE mobile = %s "
            "AND updated_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') "
            "RETURNING draft_ref, doc_path",
            (mobile, DRAFT_RETENTION_DAYS),
        )
        expired = cur.fetchone()
        if expired:
            cur.execute(
                "DELETE FROM citizen_draft_reviews WHERE draft_ref = %s",
                (expired[0],),
            )

        cur.execute(
            "SELECT draft_ref, draft_data, doc_path, doc_filename, document_status, "
            "document_message, completed FROM citizen_application_drafts "
            "WHERE mobile = %s AND updated_at >= "
            "CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') FOR UPDATE",
            (mobile, DRAFT_RETENTION_DAYS),
        )
        row = cur.fetchone()
        if row:
            if row[6]:
                old_draft_ref, old_doc_path = row[0], row[2]
                draft_ref = secrets.token_urlsafe(32)
                cur.execute(
                    "DELETE FROM citizen_draft_reviews WHERE draft_ref = %s",
                    (old_draft_ref,),
                )
                cur.execute(
                    "UPDATE citizen_application_drafts SET draft_ref = %s, "
                    "draft_data = %s::jsonb, doc_path = NULL, doc_id = NULL, "
                    "doc_filename = NULL, document_status = 'PENDING_UPLOAD', "
                    "document_message = NULL, completed = FALSE, "
                    "created_at = %s, updated_at = %s WHERE mobile = %s",
                    (
                        draft_ref,
                        json.dumps(updates),
                        portal.now_ist(),
                        portal.now_ist(),
                        mobile,
                    ),
                )
                conn.commit()
                if old_doc_path:
                    _remove_expired_draft_file(old_doc_path)
                return jsonify({
                    "draft_ref": draft_ref,
                    "details": {
                        key: _mask_bank_account(value) if key == "bank_account" else value
                        for key, value in updates.items()
                    },
                    "document_status": "PENDING_UPLOAD",
                    "document_attached": False,
                    "draft_status": "DRAFT",
                    "next_step": "attach_age_proof",
                    "message": (
                        "A new draft was created because the previous application "
                        "was withdrawn. Final submission requires an age-proof "
                        "document and officer validation."
                    ),
                }), 201
            draft_data = _draft_data(row[1])
            draft_data.update(updates)
            cur.execute(
                "UPDATE citizen_application_drafts SET draft_data = %s::jsonb, "
                "updated_at = %s WHERE mobile = %s",
                (json.dumps(draft_data), portal.now_ist(), mobile),
            )
            conn.commit()
            if expired and expired[1]:
                _remove_expired_draft_file(expired[1])
            row = (row[0], draft_data, row[2], row[3], row[4], row[5], row[6])
            return jsonify(_draft_response(row))

        draft_ref = secrets.token_urlsafe(32)
        cur.execute(
            "INSERT INTO citizen_application_drafts (mobile, draft_ref, draft_data) "
            "VALUES (%s, %s, %s::jsonb)",
            (mobile, draft_ref, json.dumps(updates)),
        )
        conn.commit()
        if expired and expired[1]:
            _remove_expired_draft_file(expired[1])
        return jsonify({
            "draft_ref": draft_ref,
            "details": {
                key: _mask_bank_account(value) if key == "bank_account" else value
                for key, value in updates.items()
            },
            "document_status": "PENDING_UPLOAD",
            "document_attached": False,
            "draft_status": "DRAFT",
            "next_step": "attach_age_proof",
            "message": (
                "Draft saved. You may continue now or return later; final submission "
                "requires an age-proof document and officer validation."
            ),
        }), 201
    finally:
        conn.close()


@agent_api.get("/api/agent/citizen/application-drafts/<draft_ref>")
def read_citizen_draft(draft_ref):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        row = _read_owned_draft(cur, mobile, draft_ref)
        if not row:
            conn.commit()
            return jsonify(error="Draft not found for this authenticated citizen."), 404
        conn.commit()
        return jsonify(_draft_response(row))
    finally:
        conn.close()


@agent_api.patch("/api/agent/citizen/application-drafts/<draft_ref>")
def save_citizen_draft(draft_ref):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    data, error = _json_object()
    if error:
        return error
    updates, error_message = _clean_draft_updates(data)
    if error_message:
        return jsonify(error=error_message), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        existing_application = portal.active_application(cur, mobile)
        if existing_application:
            conn.rollback()
            return jsonify(
                error=(
                    "An application in a status other than REJECTED or WITHDRAWN "
                    "already exists for this verified mobile number."
                ),
                application_number=existing_application[1],
                status=existing_application[2],
            ), 409
        row = _read_owned_draft(cur, mobile, draft_ref, for_update=True)
        if not row:
            conn.rollback()
            return jsonify(error="Draft not found for this authenticated citizen."), 404
        if row[6]:
            conn.rollback()
            return jsonify(error="This draft has already been submitted."), 409
        draft_data = _draft_data(row[1])
        draft_data.update(updates)
        cur.execute(
            "UPDATE citizen_application_drafts SET draft_data = %s::jsonb, "
            "updated_at = %s WHERE mobile = %s AND draft_ref = %s",
            (json.dumps(draft_data), portal.now_ist(), mobile, draft_ref),
        )
        conn.commit()
        return jsonify(_draft_response(
            (row[0], draft_data, row[2], row[3], row[4], row[5], row[6])
        ))
    finally:
        conn.close()


@agent_api.post("/api/agent/citizen/application-drafts/<draft_ref>/age-proof")
def attach_citizen_age_proof(draft_ref):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    if request.mimetype == "multipart/form-data":
        uploaded_file = request.files.get("document")
        if uploaded_file is None or not uploaded_file.filename:
            return jsonify(error="Attach the actual age-proof file as 'document'."), 400
        filename = uploaded_file.filename
        try:
            document = uploaded_file.stream.read(MAX_DOCUMENT_BYTES + 1)
        except OSError:
            portal.app.logger.exception("citizen draft age-proof upload read failed")
            return jsonify(error="The attached file could not be read. Please retry."), 400
    elif request.is_json:
        data, error = _json_object()
        if error:
            return error
        filename = data.get("filename")
        encoded_document = data.get("file_base64")
        if not isinstance(filename, str) or not isinstance(encoded_document, str):
            return jsonify(
                error="The actual age-proof file and its filename are required."
            ), 400
        if len(encoded_document) > ((MAX_DOCUMENT_BYTES + 2) // 3) * 4:
            return jsonify(error="Age-proof document must be 5 MB or smaller."), 413
        try:
            document = _timed_operation(
                "age_proof_base64_decode",
                base64.b64decode,
                encoded_document,
                validate=True,
            )
        except (binascii.Error, ValueError):
            return jsonify(error="Age-proof document is not valid base64."), 400
    else:
        return jsonify(error="Upload the document as multipart form data or JSON."), 415

    if not isinstance(filename, str):
        return jsonify(error="The age-proof filename must be text."), 400
    extension = os.path.splitext(
        os.path.basename(filename.replace("\\", "/"))
    )[1].lower()
    if extension not in DOCUMENT_EXTENSIONS:
        return jsonify(error="Age proof must be a PDF, JPG or PNG file."), 400
    if not document:
        return jsonify(error="Age-proof document cannot be empty."), 400
    if len(document) > MAX_DOCUMENT_BYTES:
        return jsonify(error="Age-proof document must be 5 MB or smaller."), 413
    if not portal.proof_content_matches(extension, document[:8]):
        return jsonify(error="The file content does not match its PDF, JPG or PNG extension."), 400
    if not _timed_operation(
        "age_proof_readability_check",
        portal.proof_content_is_readable,
        extension,
        document,
    ):
        return jsonify(
            error="The age-proof file cannot be opened or is unreadable. Upload a valid file."
        ), 400

    conn = portal.get_db()
    document_path = None
    previous_path = None
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        row = _read_owned_draft(cur, mobile, draft_ref, for_update=True)
        if not row:
            conn.rollback()
            return jsonify(error="Draft not found for this authenticated citizen."), 404
        if row[6]:
            conn.rollback()
            return jsonify(error="This draft has already been submitted."), 409
        previous_path = row[2]
        document_id = secrets.token_urlsafe(24)
        os.makedirs(portal.UPLOAD_DIR, mode=0o700, exist_ok=True)
        document_path = os.path.join(
            portal.UPLOAD_DIR,
            "citizen_draft_%s%s" % (secrets.token_hex(24), extension),
        )
        try:
            descriptor = os.open(
                document_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as document_file:
                document_file.write(document)
            if not _timed_operation(
                "age_proof_storage_readback",
                _stored_document_matches_upload,
                document_path,
                document,
            ):
                raise OSError("Stored age-proof file did not match the uploaded bytes.")
        except OSError:
            conn.rollback()
            _remove_expired_draft_file(document_path)
            portal.app.logger.exception("citizen draft age-proof storage failed")
            return jsonify(
                error="The age-proof upload could not be stored. The draft is unchanged; please retry."
            ), 503
        display_filename = secure_filename(filename.replace("\\", "/").split("/")[-1])
        cur.execute(
            "UPDATE citizen_application_drafts SET doc_path = %s, doc_id = %s, doc_filename = %s, "
            "document_status = 'PENDING_VALIDATION', document_message = %s, updated_at = %s "
            "WHERE mobile = %s AND draft_ref = %s",
            (
                document_path,
                document_id,
                (display_filename or "age-proof" + extension)[:255],
                "Document received; validation is pending.",
                portal.now_ist(),
                mobile,
                draft_ref,
            ),
        )
        conn.commit()
        if previous_path and previous_path != document_path:
            _remove_expired_draft_file(previous_path)
        return jsonify({
            "draft_ref": draft_ref,
            "document_id": document_id,
            "document_status": "PENDING_VALIDATION",
            "message": (
                "The age-proof file was received and stored securely. Validation is "
                "pending; this does not establish document authenticity."
            ),
        }), 201
    except Exception:
        conn.rollback()
        if document_path:
            _remove_expired_draft_file(document_path)
        raise
    finally:
        conn.close()


@agent_api.post("/api/agent/citizen/application-drafts/<draft_ref>/validate-age-proof")
def validate_citizen_age_proof(draft_ref):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        row = _read_owned_draft(cur, mobile, draft_ref, for_update=True)
        if not row:
            conn.commit()
            return jsonify(error="Draft not found for this authenticated citizen."), 404
        if row[6]:
            conn.rollback()
            return jsonify(error="This draft has already been submitted."), 409
        if not row[2]:
            conn.commit()
            return jsonify({
                "draft_ref": draft_ref,
                "document_status": "PENDING_UPLOAD",
                "message": "Attach an age-proof file before validation.",
            }), 409
        if not _timed_operation(
            "age_proof_validation",
            portal.valid_uploaded_proof,
            row[2],
        ):
            cur.execute(
                "UPDATE citizen_application_drafts SET document_status = 'INVALID', "
                "document_message = %s, updated_at = %s "
                "WHERE mobile = %s AND draft_ref = %s",
                (
                    "The stored document is missing, unreadable as a supported file, or invalid.",
                    portal.now_ist(),
                    mobile,
                    draft_ref,
                ),
            )
            conn.commit()
            return jsonify({
                "draft_ref": draft_ref,
                "document_status": "INVALID",
                "message": "The file failed supported-file validation. Attach a corrected document.",
            }), 200
        message = (
            "File type, signature, storage, and size passed. Name, age evidence, and "
            "authenticity require authorized officer review."
        )
        cur.execute(
            "UPDATE citizen_application_drafts SET document_status = 'REVIEW_REQUIRED', "
            "document_message = %s, updated_at = %s "
            "WHERE mobile = %s AND draft_ref = %s",
            (message, portal.now_ist(), mobile, draft_ref),
        )
        conn.commit()
        return jsonify({
            "draft_ref": draft_ref,
            "document_status": "REVIEW_REQUIRED",
            "message": message,
        })
    finally:
        conn.close()


@agent_api.post("/api/agent/citizen/application-drafts/<draft_ref>/submit")
def submit_citizen_draft(draft_ref):
    auth, error = _authorized("citizen")
    if error:
        return error
    _, mobile = auth
    data, error = _json_object()
    if error:
        return error
    if data.get("declaration_confirmed") is not True:
        return jsonify(error="The applicant must review and confirm the declaration."), 400
    if data.get("summary_confirmed") is not True:
        return jsonify(error="The applicant must confirm the displayed application summary."), 400

    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))
        row = _read_owned_draft(cur, mobile, draft_ref, for_update=True)
        if not row:
            conn.rollback()
            return jsonify(error="Draft not found for this authenticated citizen."), 404
        draft_age_ms = row[8]
        submission_continued_at = time.perf_counter()
        existing = portal.active_application(cur, mobile, lock=False)
        if existing:
            conn.rollback()
            return jsonify({
                "application_id": existing[0],
                "application_number": existing[1],
                "status": existing[2],
                "message": "An application already exists; no new application was created.",
            }), 200
        if row[6]:
            conn.rollback()
            return jsonify(error="This draft has already been submitted."), 409
        draft_data = _draft_data(row[1])
        if draft_data.get("block") not in blocks:
            conn.rollback()
            return jsonify(
                error="Agent applications are not enabled for this block.",
                enabled_blocks=blocks,
            ), 400
        if row[4] != "VALID":
            conn.rollback()
            message = row[5] or "Age proof must be verified by an authorized officer."
            return jsonify(
                error="Age-proof document is not approved for submission.",
                document_status=row[4],
                document_message=message,
            ), 409
        if not _timed_operation(
            "submission_document_revalidation",
            portal.valid_uploaded_proof,
            row[2],
        ):
            conn.rollback()
            return jsonify(
                error="The stored age-proof document is missing or failed file validation.",
                document_status="INVALID",
            ), 409
        draft_data["dob"] = draft_data.pop("dob", "")
        draft_data["age_proof_filename"] = row[3]
        errors, cleaned = _timed_operation(
            "submission_application_validation",
            portal.validate_application,
            draft_data,
            row[2],
        )
        if errors:
            conn.rollback()
            return jsonify(
                error="Application validation failed.",
                validation_errors=errors,
            ), 400
        application_id, application_number = _timed_operation(
            "submission_create_application",
            portal.create_application,
            cur,
            mobile,
            cleaned,
        )
        cur.execute(
            "UPDATE citizen_application_drafts SET completed = TRUE, updated_at = %s "
            "WHERE mobile = %s AND draft_ref = %s",
            (portal.now_ist(), mobile, draft_ref),
        )
        conn.commit()
        status = "PENDING"
        submitted_at = portal.now_ist().isoformat()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    _timed_operation(
        "submission_confirmation_sms",
        portal.send_sms,
        mobile,
        "Sewa Setu: application %s received. Track it at the official status portal."
        % application_number,
    )
    portal.app.logger.info(
        "application_lifecycle outcome=submitted total_ms=%.2f",
        float(draft_age_ms)
        + (time.perf_counter() - submission_continued_at) * 1000,
    )
    return jsonify({
        "application_id": application_id,
        "application_number": application_number,
        "status": status,
        "block": cleaned["block"],
        "submitted_at": submitted_at,
        "message": "The portal accepted and recorded the application.",
        "slip_available": True,
        "slip_tool": "get_my_application_slip",
    }), 201


@agent_api.post("/api/agent/citizen/applications")
def submit_citizen_application():
    auth, error = _authorized("citizen")
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    if data.get("declaration_confirmed") is not True:
        return jsonify(error="The applicant must review and confirm the declaration."), 400
    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500
    block = (data.get("block") or "").strip()
    if block not in blocks:
        return jsonify(
            error="Agent applications are not enabled for this block.",
            enabled_blocks=blocks,
        ), 400
    has_document = (
        isinstance(data.get("age_proof_filename"), str)
        and isinstance(data.get("age_proof_base64"), str)
    )
    errors, _ = portal.validate_application(
        data,
        "untrusted-file-not-validated" if has_document else "",
    )
    if errors:
        return jsonify(
            error="Application validation failed.",
            validation_errors=errors,
        ), 400
    return jsonify(
        error="Direct submission is disabled. Create or resume a draft and submit it through the validated draft workflow."
    ), 409


@agent_api.get("/api/agent/officer/age-proof-review-drafts")
def officer_age_proof_review_drafts():
    _, blocks, error = _officer_scope()
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        cur.execute(
            "SELECT draft_ref, draft_data, doc_filename, document_status, updated_at "
            "FROM citizen_application_drafts WHERE document_status = 'REVIEW_REQUIRED' "
            "AND completed = FALSE AND draft_data->>'block' = ANY(%s) "
            "AND updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') "
            "ORDER BY updated_at ASC LIMIT 50",
            (blocks, DRAFT_RETENTION_DAYS),
        )
        drafts = cur.fetchall()
        conn.commit()
        return jsonify({
            "drafts": [
                {
                    "draft_ref": row[0],
                    "details": {
                        **_draft_data(row[1]),
                        "bank_account": _mask_bank_account(
                            _draft_data(row[1]).get("bank_account")
                        ),
                    },
                    "age_proof_filename": row[2],
                    "document_status": row[3],
                    "updated_at": _format_date(row[4]),
                }
                for row in drafts
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/age-proof-review-drafts/<draft_ref>/document")
def officer_age_proof_review_document(draft_ref):
    _, blocks, error = _officer_scope()
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        cur.execute(
            "SELECT draft_data, doc_path, doc_filename, document_status, doc_id "
            "FROM citizen_application_drafts WHERE draft_ref = %s AND "
            "updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')",
            (draft_ref, DRAFT_RETENTION_DAYS),
        )
        row = cur.fetchone()
        conn.commit()
        if not row or _draft_data(row[0]).get("block") not in blocks:
            return jsonify(error="Draft not found in the officer's enabled blocks."), 404
        if row[3] != "REVIEW_REQUIRED":
            return jsonify(error="This document is not awaiting officer review."), 409
        if not portal.valid_uploaded_proof(row[1]):
            return jsonify(error="The stored age-proof file is missing or invalid."), 409
        with open(row[1], "rb") as document_file:
            contents = document_file.read(MAX_DOCUMENT_BYTES + 1)
        if len(contents) > MAX_DOCUMENT_BYTES:
            return jsonify(error="The stored document exceeds the allowed size."), 409
        return jsonify({
            "draft_ref": draft_ref,
            "document_id": row[4],
            "filename": row[2],
            "document_status": row[3],
            "file_base64": base64.b64encode(contents).decode("ascii"),
        })
    finally:
        conn.close()


@agent_api.post("/api/agent/officer/age-proof-review-drafts/<draft_ref>/decision")
def officer_age_proof_review_decision(draft_ref):
    auth, blocks, error = _officer_scope()
    if error:
        return error
    officer = auth
    data, error = _json_object()
    if error:
        return error
    raw_decision = data.get("decision")
    reason = data.get("reason")
    document_id = data.get("document_id")
    if not isinstance(raw_decision, str) or not isinstance(reason, str):
        return jsonify(error="A text decision and review reason are required."), 400
    decision = raw_decision.strip().upper()
    reason = reason.strip()
    if decision not in ("VALID", "INVALID", "REVIEW_REQUIRED"):
        return jsonify(error="decision must be VALID, INVALID, or REVIEW_REQUIRED."), 400
    if not reason or len(reason) > 500:
        return jsonify(error="A review reason of 1 to 500 characters is required."), 400
    if not isinstance(document_id, str) or not document_id:
        return jsonify(error="The current document_id from the review file is required."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        _ensure_citizen_drafts_table(cur)
        cur.execute(
            "SELECT mobile, draft_data, doc_path, document_status, doc_id "
            "FROM citizen_application_drafts WHERE draft_ref = %s AND "
            "updated_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day') FOR UPDATE",
            (draft_ref, DRAFT_RETENTION_DAYS),
        )
        row = cur.fetchone()
        if (
            not row
            or _draft_data(row[1]).get("block") not in blocks
            or not row[2]
            or row[3] not in ("REVIEW_REQUIRED", "INVALID")
            or row[4] != document_id
        ):
            conn.rollback()
            return jsonify(error="Draft not found or not eligible for document review."), 404
        if decision == "VALID" and not portal.valid_uploaded_proof(row[2]):
            conn.rollback()
            return jsonify(error="The stored age-proof file failed file validation."), 409
        cur.execute(
            "UPDATE citizen_application_drafts SET document_status = %s, "
            "document_message = %s, updated_at = %s "
            "WHERE draft_ref = %s AND doc_id = %s",
            (decision, reason, portal.now_ist(), draft_ref, document_id),
        )
        cur.execute(
            "INSERT INTO citizen_draft_reviews "
            "(draft_ref, officer, decision, reason, reviewed_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (draft_ref, officer, decision, reason, portal.now_ist()),
        )
        conn.commit()
        return jsonify({
            "draft_ref": draft_ref,
            "document_status": decision,
            "reviewed_by": officer,
            "message": "Document review recorded.",
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applications")
def officer_pending_applications():
    auth, error = _authorized("officer")
    if error:
        return error
    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500

    try:
        limit = int(request.args.get("limit", "20"))
    except ValueError:
        return jsonify(error="limit must be an integer between 1 and 50."), 400
    if not 1 <= limit <= 50:
        return jsonify(error="limit must be an integer between 1 and 50."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no, applicant_name, village, block, "
            "submitted_at FROM applications WHERE status = 'PENDING' "
            "AND block = ANY(%s) ORDER BY submitted_at ASC LIMIT %s",
            (blocks, limit),
        )
        rows = cur.fetchall()
        return jsonify({
            "enabled_blocks": blocks,
            "applications": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "village": row[3],
                    "block": row[4],
                    "submitted_at": _format_date(row[5]),
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/approved-applications")
def officer_approved_applications():
    _, blocks, error = _officer_scope()
    if error:
        return error
    limit, error = _query_integer("limit", 20, 1, 50)
    if error:
        return error
    offset, error = _query_integer("offset", 0, 0, 1000000)
    if error:
        return error

    requested_block = (request.args.get("block") or "").strip()
    if requested_block and requested_block not in blocks:
        return jsonify(error="The requested block is not agent-enabled."), 403
    selected_blocks = [requested_block] if requested_block else blocks

    parsed_dates = {}
    for field, column, is_end in (
        ("submitted_from", "a.submitted_at", False),
        ("submitted_to", "a.submitted_at", True),
        ("decision_from", "a.decided_at", False),
        ("decision_to", "a.decided_at", True),
    ):
        raw_value = (request.args.get(field) or "").strip()
        if not raw_value:
            continue
        try:
            parsed = datetime.strptime(raw_value, "%Y-%m-%d").date()
        except ValueError:
            return jsonify(error="%s must be a date in YYYY-MM-DD format." % field), 400
        if parsed.isoformat() != raw_value:
            return jsonify(error="%s must be a date in YYYY-MM-DD format." % field), 400
        if is_end and parsed == parsed.max:
            return jsonify(error="%s must be before 9999-12-31." % field), 400
        parsed_dates[field] = parsed

    filter_ranges = (
        ("submitted_from", "submitted_to"),
        ("decision_from", "decision_to"),
    )
    for start_field, end_field in filter_ranges:
        if (
            start_field in parsed_dates
            and end_field in parsed_dates
            and parsed_dates[start_field] > parsed_dates[end_field]
        ):
            return jsonify(
                error="%s cannot be later than %s." % (start_field, end_field)
            ), 400

    date_columns = {
        "submitted_from": "a.submitted_at",
        "submitted_to": "a.submitted_at",
        "decision_from": "a.decided_at",
        "decision_to": "a.decided_at",
    }
    conditions = ["a.status = 'APPROVED'", "a.block = ANY(%s)"]
    params = [selected_blocks]
    for field, column in date_columns.items():
        if field not in parsed_dates:
            continue
        is_end = field.endswith("_to")
        value = parsed_dates[field] + timedelta(days=1) if is_end else parsed_dates[field]
        conditions.append(column + (" < %s" if is_end else " >= %s"))
        params.append(value)
    where_clause = " AND ".join(conditions)

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT COUNT(*) FROM applications a WHERE " + where_clause,
            tuple(params),
        )
        total_count = cur.fetchone()[0]
        cur.execute(
            "SELECT a.id, a.application_no, a.applicant_name, a.dob, a.gender, "
            "a.village, a.block, a.submitted_at, a.decided_at, a.decided_by "
            "FROM applications a WHERE " + where_clause
            + " ORDER BY a.decided_at DESC NULLS LAST, a.id DESC "
            "LIMIT %s OFFSET %s",
            tuple(params + [limit, offset]),
        )
        rows = cur.fetchall()
        application_ids = [row[0] for row in rows]
        audit_by_application = {application_id: [] for application_id in application_ids}
        if application_ids:
            cur.execute(
                "SELECT application_id, action, at, actor, note FROM audit_log "
                "WHERE application_id = ANY(%s) "
                "AND UPPER(action) IN ('APPROVED', 'REJECTED', 'DEEMED_APPROVED') "
                "ORDER BY at ASC NULLS FIRST, id ASC",
                (application_ids,),
            )
            for app_id, action, at, actor, note in cur.fetchall():
                audit_by_application[app_id].append({
                    "decision": action,
                    "at": _format_date(at),
                    "officer": actor,
                    "reason": portal.mask_sensitive_numbers(note or ""),
                })

        applications = []
        for row in rows:
            history = audit_by_application[row[0]]
            approvals = [
                item for item in history
                if item["decision"].upper() == "APPROVED"
            ]
            applications.append({
                "application_id": row[0],
                "application_number": row[1],
                "applicant_name": row[2],
                "date_of_birth": _format_date(row[3]),
                "gender": row[4],
                "village": row[5],
                "block": row[6],
                "submitted_at": _format_date(row[7]),
                "status": "APPROVED",
                "approved_at": _format_date(row[8]),
                "approving_officer": row[9],
                "approval_reason": approvals[-1]["reason"] if approvals else None,
                "decision_history": history,
            })
        return jsonify({
            "enabled_blocks": blocks,
            "filters": {
                "block": requested_block or None,
                "submitted_from": request.args.get("submitted_from") or None,
                "submitted_to": request.args.get("submitted_to") or None,
                "decision_from": request.args.get("decision_from") or None,
                "decision_to": request.args.get("decision_to") or None,
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total_count": total_count,
                "has_more": offset + len(applications) < total_count,
            },
            "applications": applications,
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applications/<int:application_id>")
def officer_application(application_id):
    auth, error = _authorized("officer")
    if error:
        return error
    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, gender, "
            "marital_status, village, block, bank_account, ifsc, doc_path, status, "
            "submitted_at, decided_at, decided_by FROM applications "
            "WHERE id = %s AND block = ANY(%s)",
            (application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404
        cur.execute(
            "SELECT at, action, actor, note FROM audit_log "
            "WHERE application_id = %s ORDER BY at ASC",
            (application_id,),
        )
        audit = cur.fetchall()
        return jsonify({
            "application_id": row[0],
            "application_number": row[1],
            "applicant_name": row[2],
            "mobile": row[3],
            "date_of_birth": _format_date(row[4]),
            "gender": row[5],
            "marital_status": row[6],
            "village": row[7],
            "block": row[8],
            "bank_account": _mask_bank_account(row[9]),
            "ifsc": row[10],
            "age_proof_filename": os.path.basename(row[11]) if row[11] else None,
            "age_proof_available": bool(row[11] and os.path.isfile(row[11])),
            "status": row[12],
            "submitted_at": _format_date(row[13]),
            "decided_at": _format_date(row[14]),
            "decided_by": row[15],
            "audit_trail": [
                {
                    "at": _format_date(item[0]),
                    "action": item[1],
                    "actor": item[2],
                    "reason": portal.mask_sensitive_numbers(item[3]),
                }
                for item in audit
            ],
        })
    finally:
        conn.close()


@agent_api.post("/api/agent/officer/applications/<int:application_id>/decision")
def officer_decision(application_id):
    auth, error = _authorized("officer")
    if error:
        return error
    _, officer = auth
    data, error = _json_object()
    if error:
        return error
    decision = (data.get("decision") or "").strip().lower()
    reason = (data.get("reason") or "").strip()
    if decision not in ("approve", "reject"):
        return jsonify(error="decision must be approve or reject."), 400
    if not reason:
        return jsonify(error="A reason is required for every officer decision."), 400
    if len(reason) > 500:
        return jsonify(error="Decision reason must be 500 characters or fewer."), 400

    try:
        blocks = enabled_blocks()
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 500
    status = "APPROVED" if decision == "approve" else "REJECTED"
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
            "WHERE id = %s AND status = 'PENDING' AND block = ANY(%s) "
            "RETURNING application_no, mobile",
            (status, datetime.now(), officer, application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify(
                error="Application is not pending or is outside the agent-enabled blocks."
            ), 409
        actor = "officer:%s" % officer
        portal.write_audit(cur, application_id, status, actor, reason)
        conn.commit()
    finally:
        conn.close()

    portal.send_sms(
        row[1],
        "Sewa Setu: application %s has been %s."
        % (row[0], status.lower()),
    )
    return jsonify({
        "application_number": row[0],
        "status": status,
        "decided_by": officer,
        "reason": reason,
    })


@agent_api.get("/api/agent/officer/sla-risk")
def officer_sla_risk_applications():
    _, blocks, error = _officer_scope()
    if error:
        return error
    days_threshold, error = _query_integer(
        "days_threshold", 3, 0, portal.SLA_DAYS
    )
    if error:
        return error
    limit, error = _query_integer("limit", 50, 1, 50)
    if error:
        return error

    now = portal.now_ist()
    minimum_age = portal.SLA_DAYS - days_threshold
    cutoff = now - timedelta(days=minimum_age)
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, village, block, "
            "submitted_at FROM applications WHERE status = 'PENDING' "
            "AND block = ANY(%s) AND submitted_at <= %s "
            "AND NOT EXISTS (SELECT 1 FROM application_flags f "
            "WHERE f.application_id = applications.id) "
            "ORDER BY submitted_at ASC LIMIT %s",
            (blocks, cutoff, limit),
        )
        rows = cur.fetchall()
        return jsonify({
            "sla_days": portal.SLA_DAYS,
            "days_threshold": days_threshold,
            "minimum_queue_age_days": minimum_age,
            "applications": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "mobile": row[3],
                    "village": row[4],
                    "block": row[5],
                    "submitted_at": _format_date(row[6]),
                    "days_in_queue": max(0, (now - row[6]).days) if row[6] else None,
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/deemed-approved")
def officer_deemed_approved_applications():
    _, blocks, error = _officer_scope()
    if error:
        return error
    days_back, error = _query_integer("days_back", 30, 1, 3650)
    if error:
        return error
    limit, error = _query_integer("limit", 50, 1, 50)
    if error:
        return error
    cutoff = portal.now_ist() - timedelta(days=days_back)

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            """SELECT a.id, a.application_no, a.applicant_name, a.village, a.block,
                      a.status, a.decided_by,
                      COALESCE((
                          SELECT MAX(al.at) FROM audit_log al
                          WHERE al.application_id = a.id
                            AND UPPER(al.action) IN ('DEEMED', 'DEEMED_APPROVED')
                      ), a.decided_at) AS deemed_at
               FROM applications a
               WHERE a.block = ANY(%s)
                 AND (
                     LOWER(COALESCE(a.decided_by, '')) IN ('deemed', 'rtps-auto')
                     OR EXISTS (
                         SELECT 1 FROM audit_log al
                         WHERE al.application_id = a.id
                           AND UPPER(al.action) IN ('DEEMED', 'DEEMED_APPROVED')
                     )
                 )
                 AND COALESCE((
                     SELECT MAX(al.at) FROM audit_log al
                     WHERE al.application_id = a.id
                       AND UPPER(al.action) IN ('DEEMED', 'DEEMED_APPROVED')
                 ), a.decided_at) >= %s
               ORDER BY deemed_at DESC NULLS LAST
               LIMIT %s""",
            (blocks, cutoff, limit),
        )
        rows = cur.fetchall()
        return jsonify({
            "days_back": days_back,
            "applications": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "village": row[3],
                    "block": row[4],
                    "status": row[5],
                    "decided_by": row[6],
                    "deemed_at": _format_date(row[7]),
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/search")
def officer_search_applications():
    _, blocks, error = _officer_scope()
    if error:
        return error
    query = (request.args.get("query") or "").strip()
    status = (request.args.get("status") or "").strip().upper()
    limit, error = _query_integer("limit", 20, 1, 50)
    if error:
        return error
    if not query or len(query) > 100:
        return jsonify(error="query must contain between 1 and 100 characters."), 400
    allowed_statuses = {
        "PENDING", "APPROVED", "REJECTED", "DEEMED_APPROVED", "WITHDRAWN"
    }
    if status and status not in allowed_statuses:
        return jsonify(error="status is not a supported application status."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        conditions = [
            "block = ANY(%s)",
            "(applicant_name ILIKE %s OR mobile = %s OR application_no = %s "
            "OR bank_account = %s)",
        ]
        params = [blocks, "%%%s%%" % query, query, query, query]
        if status:
            conditions.append("status = %s")
            params.append(status)
        params.append(limit)
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, village, block, "
            "status, submitted_at, right(bank_account, 4) FROM applications "
            "WHERE " + " AND ".join(conditions)
            + " ORDER BY submitted_at DESC NULLS LAST LIMIT %s",
            tuple(params),
        )
        rows = cur.fetchall()
        return jsonify({
            "query": portal.mask_sensitive_numbers(query),
            "status_filter": status or None,
            "applications": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "mobile": row[3],
                    "village": row[4],
                    "block": row[5],
                    "status": row[6],
                    "submitted_at": _format_date(row[7]),
                    "bank_account_last4": row[8],
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applicant-history")
def officer_applicant_history():
    _, blocks, error = _officer_scope()
    if error:
        return error
    mobile = (request.args.get("mobile") or "").strip()
    if not re.fullmatch(r"\d{10}", mobile):
        return jsonify(error="mobile must be a 10-digit number."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT id, application_no, applicant_name, mobile, dob, village, "
            "block, status, submitted_at, decided_at, decided_by "
            "FROM applications WHERE mobile = %s AND block = ANY(%s) "
            "ORDER BY submitted_at DESC NULLS LAST",
            (mobile, blocks),
        )
        rows = cur.fetchall()
        application_ids = [row[0] for row in rows]
        audits_by_application = {application_id: [] for application_id in application_ids}
        if application_ids:
            cur.execute(
                "SELECT application_id, at, action, actor, note FROM audit_log "
                "WHERE application_id = ANY(%s) ORDER BY at ASC",
                (application_ids,),
            )
            for item in cur.fetchall():
                audits_by_application[item[0]].append({
                    "at": _format_date(item[1]),
                    "action": item[2],
                    "actor": item[3],
                    "note": portal.mask_sensitive_numbers(item[4]),
                })
        return jsonify({
            "mobile": mobile,
            "applications": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "mobile": row[3],
                    "date_of_birth": _format_date(row[4]),
                    "village": row[5],
                    "block": row[6],
                    "status": row[7],
                    "submitted_at": _format_date(row[8]),
                    "decided_at": _format_date(row[9]),
                    "decided_by": row[10],
                    "audit_trail": audits_by_application[row[0]],
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applications/<int:application_id>/duplicates")
def officer_duplicate_claims(application_id):
    _, blocks, error = _officer_scope()
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT mobile, bank_account, husband_name, village FROM applications "
            "WHERE id = %s AND block = ANY(%s)",
            (application_id, blocks),
        )
        target = cur.fetchone()
        if not target:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404
        mobile, bank_account, husband_name, village = target
        cur.execute(
            """SELECT id, application_no, applicant_name, village, block, status,
                      ARRAY_REMOVE(ARRAY[
                          CASE WHEN %s <> '' AND bank_account = %s
                               THEN 'bank_account' END,
                          CASE WHEN %s <> '' AND mobile = %s
                               THEN 'mobile' END,
                          CASE WHEN %s <> '' AND %s <> ''
                                    AND LOWER(TRIM(husband_name)) = LOWER(TRIM(%s))
                                    AND LOWER(TRIM(village)) = LOWER(TRIM(%s))
                               THEN 'husband_name_and_village' END
                      ], NULL) AS matching_criteria
               FROM applications
               WHERE id <> %s AND block = ANY(%s)
                 AND (
                     (%s <> '' AND bank_account = %s)
                     OR (%s <> '' AND mobile = %s)
                     OR (%s <> '' AND %s <> ''
                         AND LOWER(TRIM(husband_name)) = LOWER(TRIM(%s))
                         AND LOWER(TRIM(village)) = LOWER(TRIM(%s)))
                 )
               ORDER BY submitted_at DESC NULLS LAST LIMIT 100""",
            (
                bank_account, bank_account, mobile, mobile,
                husband_name, village, husband_name, village,
                application_id, blocks,
                bank_account, bank_account, mobile, mobile,
                husband_name, village, husband_name, village,
            ),
        )
        rows = cur.fetchall()
        return jsonify({
            "application_id": application_id,
            "matches": [
                {
                    "application_id": row[0],
                    "application_number": row[1],
                    "applicant_name": row[2],
                    "village": row[3],
                    "block": row[4],
                    "status": row[5],
                    "matching_criteria": row[6],
                }
                for row in rows
            ],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applications/<int:application_id>/bank-verification")
def officer_bank_verification(application_id):
    _, blocks, error = _officer_scope()
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT bank_account, ifsc FROM applications "
            "WHERE id = %s AND block = ANY(%s)",
            (application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404

        bank_account, ifsc = row
        bank_account = bank_account or ""
        ifsc = (ifsc or "").strip().upper()
        account_valid = (
            bool(re.fullmatch(r"[0-9]+", bank_account))
            and len(bank_account) in portal.BANK_ACCOUNT_LENGTHS
        )
        ifsc_valid = bool(re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc))
        legacy_prefix = ifsc[:4] == "UTBI"
        return jsonify({
            "application_id": application_id,
            "account_number_valid": account_valid,
            "ifsc_format_valid": ifsc_valid,
            "ifsc_prefix": ifsc[:4] or None,
            "legacy_merged_bank_prefix": legacy_prefix,
            "overall_format_valid": account_valid and ifsc_valid,
            "requires_manual_bank_confirmation": legacy_prefix or not ifsc_valid,
            "registry_check_performed": False,
            "message": (
                "Format checks only; this tool does not verify account ownership, "
                "account activity, or live bank-branch status."
            ),
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/applications/<int:application_id>/document")
def officer_application_document(application_id):
    _, blocks, error = _officer_scope()
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT doc_path FROM applications WHERE id = %s AND block = ANY(%s)",
            (application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404
        document_path = row[0]
    finally:
        conn.close()

    if not document_path:
        return jsonify(error="No age-proof document is attached to this application."), 404
    upload_root = os.path.realpath(portal.UPLOAD_DIR)
    resolved_path = os.path.realpath(document_path)
    try:
        if os.path.commonpath((upload_root, resolved_path)) != upload_root:
            return jsonify(error="The stored document path is outside the upload directory."), 403
    except ValueError:
        return jsonify(error="The stored document path is invalid."), 403
    if not os.path.isfile(resolved_path):
        return jsonify(error="The stored age-proof document is unavailable."), 404
    extension = os.path.splitext(resolved_path)[1].lower()
    if extension not in DOCUMENT_EXTENSIONS:
        return jsonify(error="The stored age-proof document type is unsupported."), 415
    file_size = os.path.getsize(resolved_path)
    if file_size > MAX_DOCUMENT_BYTES:
        return jsonify(error="The stored age-proof document exceeds the 5 MB limit."), 413
    with open(resolved_path, "rb") as document_file:
        contents = document_file.read(MAX_DOCUMENT_BYTES + 1)
    if len(contents) > MAX_DOCUMENT_BYTES:
        return jsonify(error="The stored age-proof document exceeds the 5 MB limit."), 413
    return jsonify({
        "application_id": application_id,
        "filename": os.path.basename(resolved_path),
        "document_type": extension.lstrip(".").upper(),
        "file_size_bytes": len(contents),
        "content_base64": base64.b64encode(contents).decode("ascii"),
    })


@agent_api.post("/api/agent/officer/applications/<int:application_id>/field-note")
def officer_add_field_note(application_id):
    officer, blocks, error = _officer_scope()
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    note = data.get("note")
    verified_by = data.get("verified_by")
    if not isinstance(note, str) or not note.strip() or len(note.strip()) > 350:
        return jsonify(error="note must contain between 1 and 350 characters."), 400
    if not isinstance(verified_by, str) or not verified_by.strip():
        return jsonify(error="verified_by is required."), 400
    verified_by = verified_by.strip()
    if len(verified_by) > 80:
        return jsonify(error="verified_by must be 80 characters or fewer."), 400

    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "SELECT status FROM applications WHERE id = %s AND block = ANY(%s) "
            "FOR UPDATE",
            (application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404
        if row[0] != "PENDING":
            return jsonify(error="Field notes can only be added to pending applications."), 409
        audit_note = "Field verification by %s: %s" % (verified_by, note.strip())
        portal.write_audit(
            cur,
            application_id,
            "FIELD_NOTE",
            "officer:%s" % officer,
            audit_note,
        )
        conn.commit()
        return jsonify({
            "application_id": application_id,
            "action": "FIELD_NOTE",
            "recorded_by": officer,
            "verified_by": verified_by,
            "note": note.strip(),
        })
    finally:
        conn.close()


@agent_api.post("/api/agent/officer/applications/batch-decision")
def officer_batch_decision():
    officer, blocks, error = _officer_scope()
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    application_ids = data.get("application_ids")
    decision = data.get("decision")
    reason = data.get("reason")
    if (
        not isinstance(application_ids, list)
        or not 1 <= len(application_ids) <= 100
        or any(type(application_id) is not int or application_id <= 0
               for application_id in application_ids)
        or len(set(application_ids)) != len(application_ids)
    ):
        return jsonify(error="application_ids must contain 1 to 100 unique positive integers."), 400
    if not isinstance(decision, str) or decision.strip().lower() not in ("approve", "reject"):
        return jsonify(error="decision must be approve or reject."), 400
    decision = decision.strip().lower()
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 450:
        return jsonify(error="reason must contain between 1 and 450 characters."), 400

    status = "APPROVED" if decision == "approve" else "REJECTED"
    now = portal.now_ist()
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            "UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
            "WHERE id = ANY(%s) AND status = 'PENDING' AND block = ANY(%s) "
            "RETURNING id, application_no, mobile",
            (status, now, officer, application_ids, blocks),
        )
        rows = cur.fetchall()
        if len(rows) != len(application_ids):
            conn.rollback()
            return jsonify(
                error="Every application must be pending and in an agent-enabled block; "
                      "no decisions were recorded."
            ), 409
        for application_id, _, _ in rows:
            portal.write_audit(
                cur,
                application_id,
                status,
                "officer:%s" % officer,
                reason.strip(),
            )
        conn.commit()
    finally:
        conn.close()

    for _, application_number, mobile in rows:
        portal.send_sms(
            mobile,
            "Sewa Setu: application %s has been %s."
            % (application_number, status.lower()),
        )
    return jsonify({
        "status": status,
        "decided_by": officer,
        "reason": reason.strip(),
        "decided_count": len(rows),
        "application_numbers": [row[1] for row in rows],
    })


@agent_api.post("/api/agent/officer/applications/<int:application_id>/flag")
def officer_flag_case(application_id):
    officer, blocks, error = _officer_scope()
    if error:
        return error
    data, error = _json_object()
    if error:
        return error
    flag_type = data.get("flag_type")
    reason = data.get("reason")
    allowed_flags = {
        "NEEDS_DOC_REUPLOAD", "SUSPECTED_UNDERAGE", "ADDRESS_UNVERIFIED"
    }
    if not isinstance(flag_type, str) or flag_type.strip().upper() not in allowed_flags:
        return jsonify(
            error="flag_type must be NEEDS_DOC_REUPLOAD, SUSPECTED_UNDERAGE, "
                  "or ADDRESS_UNVERIFIED."
        ), 400
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 450:
        return jsonify(error="reason must contain between 1 and 450 characters."), 400
    flag_type = flag_type.strip().upper()
    reason = reason.strip()
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            """CREATE TABLE IF NOT EXISTS application_flags (
                   application_id INTEGER PRIMARY KEY REFERENCES applications(id),
                   flag_type VARCHAR(50) NOT NULL,
                   reason VARCHAR(500) NOT NULL,
                   flagged_by VARCHAR(50) NOT NULL,
                   flagged_at TIMESTAMP NOT NULL
               )"""
        )
        cur.execute(
            "SELECT status FROM applications WHERE id = %s AND block = ANY(%s) "
            "FOR UPDATE",
            (application_id, blocks),
        )
        row = cur.fetchone()
        if not row:
            return jsonify(error="Application not found in the agent-enabled blocks."), 404
        if row[0] != "PENDING":
            return jsonify(error="Only pending applications can be flagged."), 409
        cur.execute(
            "INSERT INTO application_flags "
            "(application_id, flag_type, reason, flagged_by, flagged_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (application_id) DO UPDATE SET "
            "flag_type = EXCLUDED.flag_type, reason = EXCLUDED.reason, "
            "flagged_by = EXCLUDED.flagged_by, flagged_at = EXCLUDED.flagged_at "
            "RETURNING application_id",
            (
                application_id, flag_type, reason, officer, portal.now_ist(),
            ),
        )
        if not cur.fetchone():
            conn.rollback()
            return jsonify(
                error="Application is not pending or is outside the agent-enabled blocks."
            ), 409
        portal.write_audit(
            cur,
            application_id,
            "CASE_FLAGGED",
            "officer:%s" % officer,
            "%s: %s" % (flag_type, reason),
        )
        conn.commit()
        return jsonify({
            "application_id": application_id,
            "flag_type": flag_type,
            "reason": reason,
            "flagged_by": officer,
            "held_from_deemed_approval": True,
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/queue-summary")
def officer_queue_summary():
    _, blocks, error = _officer_scope()
    if error:
        return error
    selected_blocks, error = _selected_blocks(
        (request.args.get("block") or "").strip(), blocks
    )
    if error:
        return error
    now = portal.now_ist()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    critical_cutoff = now - timedelta(days=12)
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            """SELECT
                   COUNT(*) FILTER (WHERE status = 'PENDING'),
                   COUNT(*) FILTER (
                       WHERE status = 'PENDING' AND submitted_at <= %s
                           AND NOT EXISTS (
                               SELECT 1 FROM application_flags f
                               WHERE f.application_id = applications.id
                           )
                       ),
                   COUNT(*) FILTER (
                       WHERE status = 'APPROVED' AND decided_at >= %s
                   ),
                   COUNT(*) FILTER (
                       WHERE status = 'REJECTED' AND decided_at >= %s
                   ),
                   COUNT(*) FILTER (
                       WHERE status = 'DEEMED_APPROVED' AND decided_at >= %s
                   )
               FROM applications WHERE block = ANY(%s)""",
            (critical_cutoff, month_start, month_start, month_start, selected_blocks),
        )
        row = cur.fetchone()
        return jsonify({
            "block": selected_blocks[0] if len(selected_blocks) == 1 else None,
            "total_pending": row[0],
            "critical_sla_count": row[1],
            "approved_this_month": row[2],
            "rejected_this_month": row[3],
            "deemed_approved_this_month": row[4],
        })
    finally:
        conn.close()


@agent_api.get("/api/agent/officer/village-breakdown")
def officer_village_breakdown():
    _, blocks, error = _officer_scope()
    if error:
        return error
    selected_blocks, error = _selected_blocks(
        (request.args.get("block") or "").strip(), blocks
    )
    if error:
        return error
    conn = portal.get_db()
    try:
        cur = _TimedCursor(conn.cursor())
        cur.execute(
            """SELECT village, block,
                      COUNT(*) FILTER (WHERE status = 'PENDING'),
                      COUNT(*) FILTER (WHERE status = 'APPROVED')
               FROM applications
               WHERE block = ANY(%s) AND status IN ('PENDING', 'APPROVED')
               GROUP BY village, block
               ORDER BY block, village""",
            (selected_blocks,),
        )
        rows = cur.fetchall()
        return jsonify({
            "block": selected_blocks[0] if len(selected_blocks) == 1 else None,
            "villages": [
                {
                    "village": row[0],
                    "block": row[1],
                    "pending_count": row[2],
                    "approved_count": row[3],
                }
                for row in rows
            ],
        })
    finally:
        conn.close()
