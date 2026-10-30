# -*- coding: utf-8 -*-
"""
Sewa Setu - Old Age Pension Portal
Government of Purvanchal, Department of Social Welfare

Developed by: Netlink Infosolutions Pvt Ltd (2023)
Maintained in-house since 02/2026.
"""

import os
import io
import re
import random
import hashlib
import configparser
import uuid
import warnings
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError, PdfStreamError

IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    """The portal's clock is Indian Standard Time regardless of where the server runs."""
    return datetime.now(IST).replace(tzinfo=None)

import requests
import psycopg2
from flask import (Flask, request, session, redirect, url_for, render_template,
                   flash, send_file, abort)
from dashboard.eval_dashboard import eval_dashboard_bp

from fpdf import FPDF

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))
config.set("database", "password", os.environ.get("DB_PASSWORD", ""))


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.register_blueprint(eval_dashboard_bp)
app_secret_key = os.environ.get("SECRET_KEY", "").strip()
if len(app_secret_key) < 32:
    raise RuntimeError("SECRET_KEY must be set to a unique value of at least 32 characters")
app.secret_key = app_secret_key
# Two hours: elderly applicants fill the form slowly; the draft lives in the session.
app.config["PERMANENT_SESSION_LIFETIME"] = 7200

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
OTP_VALIDITY_SECONDS = config.getint("app", "otp_validity_seconds")
UPLOAD_DIR = config.get("app", "upload_dir")
SMS_GATEWAY_TOKEN = os.environ.get("SMS_GATEWAY_TOKEN", "")
if len(SMS_GATEWAY_TOKEN) < 32:
    raise RuntimeError("SMS_GATEWAY_TOKEN must be set to a unique value of at least 32 characters")
SCHEME_DEADLINE = datetime.strptime(config.get("pension", "scheme_deadline"),
                                    "%Y-%m-%d %H:%M")
MIN_AGE = config.getint("pension", "min_age")
SLA_DAYS = config.getint("pension", "sla_days")

BLOCKS = ["Sonari", "Rajapara", "Dhemaji Pathar", "Borgaon", "Namti", "Khelua"]
def _bank_account_lengths():
    configured = os.getenv("BANK_ACCOUNT_LENGTHS")
    if not configured:
        return tuple(range(9, 19))
    try:
        lengths = tuple(sorted({int(item.strip()) for item in configured.split(",")}))
    except ValueError as exc:
        raise RuntimeError("BANK_ACCOUNT_LENGTHS must be a comma-separated list of integers.") from exc
    if not lengths or any(length < 1 or length > 30 for length in lengths):
        raise RuntimeError("BANK_ACCOUNT_LENGTHS must contain lengths between 1 and 30.")
    return lengths


BANK_ACCOUNT_LENGTHS = _bank_account_lengths()

import logging
_logdir = "/var/log/sewasetu"
try:
    os.makedirs(_logdir, exist_ok=True)
    _fh = logging.FileHandler(os.path.join(_logdir, "sewasetu-app.log"))
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s in app: %(message)s"))
    app.logger.addHandler(_fh)
    app.logger.setLevel(logging.INFO)
except Exception:
    pass


def get_db():
    if not os.environ.get("DB_PASSWORD"):
        raise RuntimeError("DB_PASSWORD must be set")
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", config.get("database", "host")),
        port=os.environ.get("DB_PORT", config.get("database", "port")),
        dbname=os.environ.get("DB_NAME", config.get("database", "name")),
        user=os.environ.get("DB_USER", config.get("database", "user")),
        password=os.environ["DB_PASSWORD"],
    )


def sanitize(value, maxlen=100):
    # Trim whitespace and cap at the column width, counting characters (not
    # bytes) so Assamese/Bangla names are stored intact.
    if value is None:
        return ""
    return value.strip()[:maxlen]


def mask_bank_account(value):
    account = str(value or "")
    if len(account) <= 4:
        return "*" * len(account)
    return "*" * (len(account) - 4) + account[-4:]


def mask_sensitive_numbers(value):
    if not isinstance(value, str):
        return value

    def mask(match):
        digits = re.sub(r"\D", "", match.group())
        return mask_bank_account(digits) if len(digits) >= 9 else match.group()

    return re.sub(r"(?<!\d)(?:\d[ \t-]?){8,29}\d(?!\d)", mask, value)


app.jinja_env.filters["mask_bank_account"] = mask_bank_account


def hash_password(p):
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def send_sms(mobile, text):
    try:
        response = requests.post(
            SMS_GATEWAY_URL + "/api/send",
            json={"to": mobile, "text": text},
            headers={"Authorization": "Bearer " + SMS_GATEWAY_TOKEN},
            timeout=5,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        app.logger.error("sms gateway error: %s" % e)
        return False


def deadline_remaining():
    delta = SCHEME_DEADLINE - now_ist()
    if delta.total_seconds() <= 0:
        return None
    return int(delta.total_seconds() // 3600)


def new_application_no():
    return "SSP" + datetime.now().strftime("%y") + str(random.randint(100000, 999999))


def write_audit(cur, app_id, action, actor, note=""):
    cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (app_id, action, actor, note, datetime.now()))


# ---------------------------------------------------------------------------
# Scheme rules: the single source of truth for what an application must satisfy.
# Every channel that creates an application (the web form, any API, any batch
# import) MUST go through validate_application() and create_application() so
# that no channel admits an application another channel would refuse.
# ---------------------------------------------------------------------------

def parse_dob(raw):
    if not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def age_on(dob, on):
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def validate_application(data, doc_path):
    """Validate a complete application. Returns (errors, cleaned).

    `errors` is a list of plain-language problems (empty when valid);
    `cleaned` holds the normalised values ready for create_application().
    """
    errors = []
    string_fields = (
        "applicant_name", "village", "block", "gender", "marital_status",
        "husband_name", "bank_account", "ifsc",
    )
    for field in string_fields:
        if data.get(field) is not None and not isinstance(data.get(field), str):
            errors.append("%s must be provided as text" % field.replace("_", " "))
    text = {
        field: value if isinstance(value := data.get(field), str) else ""
        for field in string_fields
    }
    cleaned = {
        "applicant_name": sanitize(text["applicant_name"]),
        "village": text["village"].strip(),
        "block": text["block"].strip(),
        "gender": text["gender"],
        "marital_status": text["marital_status"],
        "husband_name": text["husband_name"].strip(),
        "husband_employer": "",  # no longer collected (not a scheme requirement)
        "bank_account": re.sub(r"\s+", "", text["bank_account"]),
        "ifsc": re.sub(r"[\s-]", "", text["ifsc"]).upper(),
        "doc_path": doc_path or "",
        "dob": None,
    }
    for label, key in (("full name", "applicant_name"), ("village", "village"),
                       ("block", "block"), ("bank account", "bank_account")):
        if not cleaned[key]:
            errors.append("%s is required" % label)
    if len(text["applicant_name"]) > 100:
        errors.append("full name must be 100 characters or fewer")
    if len(cleaned["village"]) > 100:
        errors.append("village must be 100 characters or fewer")
    if len(cleaned["block"]) > 50:
        errors.append("block must be 50 characters or fewer")
    if len(cleaned["gender"]) > 10:
        errors.append("gender must be 10 characters or fewer")
    if len(cleaned["marital_status"]) > 20:
        errors.append("marital status must be 20 characters or fewer")
    if len(cleaned["husband_name"]) > 100:
        errors.append("husband name must be 100 characters or fewer")
    if len(cleaned["bank_account"]) > 30:
        errors.append("bank account number exceeds the maximum field length")
    if len(cleaned["ifsc"]) > 15:
        errors.append("IFSC must be 15 characters or fewer")
    if cleaned["block"] and cleaned["block"] not in BLOCKS:
        errors.append("block must be one of: " + ", ".join(BLOCKS))
    if cleaned["bank_account"]:
        if not re.fullmatch(r"[0-9]+", cleaned["bank_account"]):
            errors.append("bank account number must contain digits only")
        elif len(cleaned["bank_account"]) not in BANK_ACCOUNT_LENGTHS:
            lengths = BANK_ACCOUNT_LENGTHS
            if lengths == tuple(range(lengths[0], lengths[-1] + 1)):
                if len(cleaned["bank_account"]) < lengths[0]:
                    errors.append("bank account number must be at least %d digits" % lengths[0])
                else:
                    errors.append(
                        "bank account number must be no more than %d digits" % lengths[-1]
                    )
            else:
                errors.append(
                    "bank account number must have one of these accepted lengths: %s digits"
                    % ", ".join(str(length) for length in lengths)
                )
    if cleaned["ifsc"] and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", cleaned["ifsc"]):
        errors.append("IFSC must be 11 characters, e.g. SBIN0003077")
    dob = parse_dob(data.get("dob"))
    if dob is None:
        errors.append("date of birth must be given as DD/MM/YYYY")
    else:
        cleaned["dob"] = dob
        if age_on(dob, now_ist().date()) < MIN_AGE:
            errors.append("applicant must be %d years of age or above" % MIN_AGE)
    if not cleaned["doc_path"]:
        errors.append("age proof document is required")
    if now_ist() > SCHEME_DEADLINE:
        errors.append("the application window has closed")
    return errors, cleaned


def valid_uploaded_proof(path):
    if not path:
        return False
    upload_root = os.path.realpath(UPLOAD_DIR)
    resolved_path = os.path.realpath(path)
    try:
        if os.path.commonpath((upload_root, resolved_path)) != upload_root:
            return False
    except ValueError:
        return False
    extension = os.path.splitext(resolved_path)[1].lower()
    if extension not in (".pdf", ".jpg", ".jpeg", ".png"):
        return False
    try:
        size = os.path.getsize(resolved_path)
        with open(resolved_path, "rb") as proof_file:
            contents = proof_file.read(5 * 1024 * 1024 + 1)
    except OSError:
        return False
    return (
        os.path.isfile(resolved_path)
        and 0 < size <= 5 * 1024 * 1024
        and len(contents) == size
        and proof_content_matches(extension, contents[:8])
        and proof_content_is_readable(extension, contents)
    )


def proof_content_is_readable(extension, contents):
    try:
        if extension == ".pdf":
            reader = PdfReader(io.BytesIO(contents), strict=True)
            return not reader.is_encrypted and len(reader.pages) > 0
        if extension in (".jpg", ".jpeg", ".png"):
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(contents)) as image:
                    if image.width * image.height > 20_000_000:
                        return False
                    image.load()
            return True
    except (
        OSError,
        ValueError,
        PdfReadError,
        PdfStreamError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
    ):
        return False
    return False


def proof_content_matches(extension, content):
    signatures = {
        ".pdf": content.startswith(b"%PDF-"),
        ".jpg": content.startswith(b"\xff\xd8\xff"),
        ".jpeg": content.startswith(b"\xff\xd8\xff"),
        ".png": content.startswith(b"\x89PNG\r\n\x1a\n"),
    }
    return signatures.get(extension, False)


def ensure_web_drafts_table(cur):
    cur.execute(
        """CREATE TABLE IF NOT EXISTS web_application_drafts (
               mobile VARCHAR(15) PRIMARY KEY,
               draft_id VARCHAR(64) NOT NULL UNIQUE,
               edit_used BOOLEAN NOT NULL DEFAULT FALSE,
               reviewed BOOLEAN NOT NULL DEFAULT FALSE,
               completed BOOLEAN NOT NULL DEFAULT FALSE,
               updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    cur.execute(
        "ALTER TABLE web_application_drafts ADD COLUMN IF NOT EXISTS "
        "reviewed BOOLEAN NOT NULL DEFAULT FALSE"
    )


def get_or_create_web_draft(mobile):
    conn = get_db()
    try:
        cur = conn.cursor()
        ensure_web_drafts_table(cur)
        cur.execute(
            """INSERT INTO web_application_drafts (mobile, draft_id)
               VALUES (%s, %s)
               ON CONFLICT (mobile) DO UPDATE SET
                   draft_id = CASE WHEN web_application_drafts.completed
                                   THEN EXCLUDED.draft_id
                                   ELSE web_application_drafts.draft_id END,
                   edit_used = CASE WHEN web_application_drafts.completed
                                    THEN FALSE ELSE web_application_drafts.edit_used END,
                   reviewed = CASE WHEN web_application_drafts.completed
                                   THEN FALSE ELSE web_application_drafts.reviewed END,
                   completed = FALSE,
                   updated_at = CURRENT_TIMESTAMP
               RETURNING draft_id, reviewed, edit_used, completed""",
            (mobile, uuid.uuid4().hex),
        )
        row = cur.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def web_draft_state(mobile, draft_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        ensure_web_drafts_table(cur)
        cur.execute(
            "SELECT reviewed, edit_used, completed FROM web_application_drafts "
            "WHERE mobile = %s AND draft_id = %s",
            (mobile, draft_id),
        )
        row = cur.fetchone()
        conn.commit()
        return row
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_web_draft_reviewed(mobile, draft_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        ensure_web_drafts_table(cur)
        cur.execute(
            """UPDATE web_application_drafts
               SET reviewed = TRUE, updated_at = CURRENT_TIMESTAMP
               WHERE mobile = %s AND draft_id = %s AND completed = FALSE""",
            (mobile, draft_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_web_draft_edit(mobile, draft_id):
    conn = get_db()
    try:
        cur = conn.cursor()
        ensure_web_drafts_table(cur)
        cur.execute(
            """UPDATE web_application_drafts
               SET edit_used = TRUE, updated_at = CURRENT_TIMESTAMP
               WHERE mobile = %s AND draft_id = %s
                 AND edit_used = FALSE AND completed = FALSE
               RETURNING draft_id""",
            (mobile, draft_id),
        )
        saved = cur.fetchone() is not None
        if saved:
            conn.commit()
        else:
            conn.rollback()
        return saved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_session_web_draft():
    mobile = session.get("verified_mobile")
    if not mobile:
        return None
    draft_id = session.get("web_draft_id")
    if not draft_id:
        draft_id, reviewed, edit_used, completed = get_or_create_web_draft(mobile)
        session["web_draft_id"] = draft_id
        session["web_reviewed"] = reviewed
        session["web_edit_used"] = edit_used
        if completed:
            return None
    state = web_draft_state(mobile, draft_id)
    if not state or state[2]:
        return None
    session["web_reviewed"] = state[0]
    session["web_edit_used"] = state[1]
    return state


def active_application(cur, mobile, *, lock=True):
    """The latest application that blocks another submission, if any.

    Lock each verified mobile through the transaction unless the caller already
    holds that lock. Rejected and withdrawn applications do not block a
    replacement; every other status does.
    """
    if lock:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (mobile,))
    cur.execute("SELECT id, application_no, status FROM applications "
                "WHERE mobile = %s AND "
                "(status IS NULL OR status NOT IN ('REJECTED', 'WITHDRAWN')) "
                "ORDER BY submitted_at DESC NULLS LAST, id DESC LIMIT 1", (mobile,))
    return cur.fetchone()


def create_application(cur, mobile, cleaned):
    """Insert a validated application; ensure the status-portal account exists.

    The caller commits. Returns (id, application_no).
    """
    app_no = new_application_no()
    cur.execute(
        """INSERT INTO applications
           (application_no, applicant_name, mobile, dob, gender, marital_status,
            husband_name, husband_employer, village, block, bank_account, ifsc,
            doc_path, status, submitted_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'PENDING',%s)
           RETURNING id""",
        (app_no, cleaned["applicant_name"], mobile, cleaned["dob"], cleaned["gender"],
         cleaned["marital_status"], cleaned["husband_name"], cleaned["husband_employer"],
         cleaned["village"], cleaned["block"], cleaned["bank_account"], cleaned["ifsc"],
         cleaned["doc_path"], datetime.now()))
    new_id = cur.fetchone()[0]

    # status portal account; password is DOB as DDMMYYYY per dept. circular
    portal_pass = cleaned["dob"].strftime("%d%m%Y")
    cur.execute("SELECT count(*) FROM portal_users WHERE mobile = %s", (mobile,))
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO portal_users (mobile, password_hash) VALUES (%s,%s)",
                    (mobile, hash_password(portal_pass)))
    write_audit(cur, new_id, "SUBMIT", "applicant:%s" % mobile)
    return new_id, app_no


# ---------------------------------------------------------------------------
# Public pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", hours_left=deadline_remaining())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/__gateway/", defaults={"subpath": ""})
@app.route("/__gateway/<path:subpath>")
def gateway_proxy(subpath):
    admin = bool(session.get("admin"))
    public_console = not subpath and not admin
    if not public_console and not admin:
        abort(403)
    # The public console is redacted; inbox and send operations remain admin-only.
    try:
        if public_console:
            r = requests.get(SMS_GATEWAY_URL + "/public", timeout=5)
        else:
            gateway_path = "/" + subpath if subpath else "/"
            r = requests.get(SMS_GATEWAY_URL + gateway_path,
                             params=request.args,
                             headers={"Authorization": "Bearer " + SMS_GATEWAY_TOKEN},
                             timeout=5)
        return (r.content, r.status_code,
                {"Content-Type": r.headers.get("Content-Type", "text/html")})
    except Exception as e:
        return ("SMS gateway unreachable: %s" % e, 502)


# ---------------------------------------------------------------------------
# Application flow: mobile -> OTP -> form steps -> upload -> declaration
# ---------------------------------------------------------------------------

@app.route("/apply", methods=["GET", "POST"])
def apply():
    if deadline_remaining() is None:
        flash("The application window for this scheme has closed.")
        return redirect(url_for("index"))
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        captcha = request.form.get("captcha", "")
        try:
            captcha_ok = int(captcha) == session.get("captcha_answer")
        except (ValueError, TypeError):
            captcha_ok = False
        if not captcha_ok:
            flash("Security check answer is incorrect. Please try again.")
            return render_template("apply.html", captcha_q=make_captcha())
        if len(mobile) != 10 or not mobile.isdigit():
            flash("Please enter a valid 10-digit mobile number.")
            return render_template("apply.html", captcha_q=make_captcha())
        code = str(random.randint(100000, 999999))
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO otps (mobile, code, created_at) VALUES (%s, %s, %s)",
                    (mobile, code, datetime.now()))
        conn.commit()
        cur.close(); conn.close()
        send_sms(mobile, "Your Sewa Setu OTP is %s. Valid for 5 minutes." % code)
        session.permanent = True
        session["apply_mobile"] = mobile
        return redirect(url_for("verify"))
    return render_template("apply.html", captcha_q=make_captcha())


def make_captcha():
    a, b = random.randint(1, 9), random.randint(1, 9)
    session["captcha_answer"] = a + b
    return "%d + %d" % (a, b)


@app.route("/verify", methods=["GET", "POST"])
def verify():
    mobile = session.get("apply_mobile")
    if not mobile:
        return redirect(url_for("apply"))
    if request.method == "POST":
        code = request.form.get("otp", "").strip()
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT code, created_at FROM otps WHERE mobile = %s "
                    "ORDER BY id DESC LIMIT 1", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == code:
            age = (datetime.now() - row[1]).total_seconds()
            if age > OTP_VALIDITY_SECONDS:
                app.logger.warning("otp expired mobile=%s age=%ds" % (mobile, int(age)))
                flash("OTP expired. Please request a new OTP.")
                return redirect(url_for("apply"))
            session["verified_mobile"] = mobile
            session.pop("web_draft_id", None)
            draft_id, reviewed, edit_used, completed = get_or_create_web_draft(mobile)
            session["web_draft_id"] = draft_id
            session["web_reviewed"] = reviewed
            session["web_edit_used"] = edit_used
            if completed:
                session["form_data"] = {}
                session.pop("doc_path", None)
                session.pop("doc_name", None)
            return redirect(url_for("form_step", step=1))
        flash("Invalid OTP.")
    return render_template("verify.html", mobile=mobile)


@app.route("/form/<int:step>", methods=["GET", "POST"])
def form_step(step):
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if step not in (1, 2, 3):
        abort(404)
    if session.get("editing_application") and not ensure_session_web_draft():
        session.pop("editing_application", None)
        session.pop("edit_form_data", None)
        flash("The one-time editing opportunity has already been used.")
        return redirect(url_for("declaration"))
    if request.method == "POST":
        editing = session.get("editing_application", False)
        draft_state = ensure_session_web_draft()
        if draft_state and draft_state[0] and not editing:
            flash("Use the one-time Edit Application button to change reviewed answers.")
            return redirect(url_for("declaration"))
        data = dict(session.get("edit_form_data" if editing else "form_data", {}))
        for k, v in request.form.items():
            data[k] = v
        session["edit_form_data" if editing else "form_data"] = data
        if step < 3:
            return redirect(url_for("form_step", step=step + 1))
        return redirect(url_for("declaration"))
    data_key = "edit_form_data" if session.get("editing_application") else "form_data"
    return render_template("form_step%d.html" % step,
                           data=session.get(data_key, {}), blocks=BLOCKS,
                           bank_account_lengths=BANK_ACCOUNT_LENGTHS,
                           editing=session.get("editing_application", False))


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if valid_uploaded_proof(session.get("doc_path", "")):
        return redirect(url_for("declaration"))
    if request.method == "POST":
        f = request.files.get("document")
        if f is None or f.filename == "":
            upload_error = "Please choose a file to upload (PDF, JPG, JPEG, or PNG, up to 5 MB)."
        else:
            extension = os.path.splitext(f.filename)[1].lower()
            if extension not in (".pdf", ".jpg", ".jpeg", ".png"):
                upload_error = "Please upload a PDF, JPG, JPEG, or PNG file."
            else:
                try:
                    content = f.read(5 * 1024 * 1024 + 1)
                except OSError:
                    app.logger.exception("proof upload read failed mobile=%s",
                                          session.get("verified_mobile"))
                    upload_error = (
                        "The selected file could not be read. Your answers are still saved; "
                        "please try another file."
                    )
                else:
                    if not content:
                        upload_error = "The selected proof document is empty. Please choose another file."
                    elif len(content) > 5 * 1024 * 1024:
                        upload_error = "File is larger than 5 MB. Please upload a smaller file."
                    elif not proof_content_matches(extension, content):
                        upload_error = (
                            "The file contents do not match the selected file type. "
                            "Please choose a valid PDF, JPG, JPEG, or PNG document."
                        )
                    else:
                        filename = "%s%s" % (uuid.uuid4().hex, extension)
                        path = os.path.join(UPLOAD_DIR, filename)
                        try:
                            os.makedirs(UPLOAD_DIR, exist_ok=True)
                            with open(path, "wb") as out:
                                out.write(content)
                            if not valid_uploaded_proof(path):
                                raise OSError("Uploaded proof file did not pass storage verification")
                        except OSError:
                            app.logger.exception("proof upload failed mobile=%s",
                                                  session.get("verified_mobile"))
                            try:
                                if os.path.exists(path):
                                    os.remove(path)
                            except OSError:
                                app.logger.exception("failed to remove incomplete proof upload")
                            upload_error = (
                                "The proof document could not be saved. Your answers are still saved; "
                                "please try uploading it again."
                            )
                        else:
                            session["doc_path"] = path
                            session["doc_name"] = os.path.basename(
                                f.filename.replace("\\", "/")
                            )[:120]
                            return redirect(url_for("declaration"))
        editing = session.get("editing_application", False)
        data = session.get("edit_form_data" if editing else "form_data", {})
        errors, _ = validate_application(data, "")
        return render_template(
            "declaration.html",
            data=data,
            errors=errors,
            doc_name="",
            upload_error=upload_error,
            editing=editing,
            edit_used=session.get("web_edit_used", False),
        )
    return render_template("upload.html")


@app.route("/declaration", methods=["GET", "POST"])
def declaration():
    if not session.get("verified_mobile"):
        flash("Session expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    draft_state = ensure_session_web_draft()
    if not draft_state:
        flash("Your application session has expired. Please verify your mobile number again.")
        return redirect(url_for("apply"))
    if request.method == "POST":
        if request.form.get("action") == "save_edit":
            return save_application_edit()
        if not draft_state[0]:
            mark_web_draft_reviewed(
                session["verified_mobile"],
                session["web_draft_id"],
            )
            flash("Please review your saved application before final submission.")
            return redirect(url_for("declaration"))
        return handle_submission()
    mark_web_draft_reviewed(
        session["verified_mobile"],
        session["web_draft_id"],
    )
    session["web_reviewed"] = True
    # Preview everything before the applicant commits.
    editing = session.get("editing_application", False)
    data = session.get("edit_form_data" if editing else "form_data", {})
    doc_path = session.get("doc_path", "")
    if not valid_uploaded_proof(doc_path):
        doc_path = ""
    errors, _ = validate_application(data, doc_path)
    return render_template("declaration.html", data=data, errors=errors,
                           doc_name=(session.get("doc_name") or os.path.basename(doc_path or "")),
                           upload_error=None,
                           editing=editing,
                           edit_used=draft_state[1])


@app.post("/application/edit")
def begin_application_edit():
    if not session.get("verified_mobile"):
        flash("Please verify your mobile number before editing the application.")
        return redirect(url_for("apply"))
    state = ensure_session_web_draft()
    if not state or not state[0] or state[1]:
        flash("Editing is available once from the final review before submission.")
        return redirect(url_for("declaration"))
    session["edit_form_data"] = dict(session.get("form_data", {}))
    session["editing_application"] = True
    return redirect(url_for("form_step", step=1))


def save_application_edit():
    state = ensure_session_web_draft()
    if not state or state[1] or not session.get("editing_application"):
        session.pop("editing_application", None)
        session.pop("edit_form_data", None)
        flash("The one-time editing opportunity is no longer available.")
        return redirect(url_for("declaration"))

    edit_data = session.get("edit_form_data", {})
    doc_path = session.get("doc_path", "")
    if not valid_uploaded_proof(doc_path):
        doc_path = ""
    errors, _ = validate_application(edit_data, doc_path)
    correction_errors = [
        error for error in errors if error != "age proof document is required"
    ]
    if correction_errors:
        return render_template(
            "declaration.html",
            data=edit_data,
            errors=correction_errors,
            doc_name=(session.get("doc_name") or os.path.basename(doc_path or "")),
            upload_error=None,
            editing=True,
            edit_used=False,
        )

    try:
        saved = save_web_draft_edit(
            session["verified_mobile"],
            session["web_draft_id"],
        )
    except Exception:
        app.logger.exception("application edit persistence failed mobile=%s",
                             session.get("verified_mobile"))
        saved = False
    if not saved:
        flash("Your corrections could not be saved. Your original answers are unchanged.")
        return redirect(url_for("declaration"))

    session["form_data"] = dict(edit_data)
    session.pop("edit_form_data", None)
    session.pop("editing_application", None)
    flash("Your corrections were saved. Review your application before submitting.")
    return redirect(url_for("declaration"))


def handle_submission():
    mobile = session.get("verified_mobile")
    data = session.get("form_data", {})
    doc_path = session.get("doc_path", "")
    if not valid_uploaded_proof(doc_path):
        doc_path = ""
    errors, cleaned = validate_application(data, doc_path)
    if errors:
        flash("Please correct the following before submitting: " + "; ".join(errors) + ".")
        return redirect(url_for("declaration"))

    conn = get_db()
    cur = conn.cursor()
    ensure_web_drafts_table(cur)
    existing = active_application(cur, mobile)
    if existing:
        cur.close(); conn.close()
        flash("Application %s (%s) already exists for this mobile number. "
              "A new application is allowed only when all previous applications "
              "are rejected or withdrawn. No new application was created."
              % (existing[1], existing[2]))
        return redirect(url_for("index"))

    new_id, app_no = create_application(cur, mobile, cleaned)
    cur.execute(
        "UPDATE web_application_drafts SET completed = TRUE, updated_at = CURRENT_TIMESTAMP "
        "WHERE mobile = %s AND draft_id = %s AND completed = FALSE RETURNING mobile",
        (mobile, session.get("web_draft_id")),
    )
    if not cur.fetchone():
        conn.rollback()
        cur.close()
        conn.close()
        flash("Your application session could not be verified. Your application was not submitted.")
        return redirect(url_for("declaration"))
    conn.commit()
    cur.close(); conn.close()

    session.pop("form_data", None)
    session.pop("doc_path", None)
    session.pop("doc_name", None)
    session.pop("web_draft_id", None)
    session.pop("web_edit_used", None)
    session.pop("web_reviewed", None)
    session.pop("editing_application", None)
    session.pop("edit_form_data", None)
    session.pop("verified_mobile", None)
    # The applicant just proved ownership of this mobile by OTP, so let them
    # see their application and download the acknowledgment without a second login.
    session["logged_in"] = True
    session["portal_mobile"] = mobile
    send_sms(mobile, "Sewa Setu: application %s received. Track at the status portal "
                     "with mobile no. and password (DOB as DDMMYYYY)." % app_no)
    return render_template("confirmation.html", app_no=app_no, app_id=new_id)


def generate_acknowledgment(cur, app_id):
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, submitted_at, status FROM applications WHERE id = %s",
                (app_id,))
    row = cur.fetchone()
    row = list(row)
    row[6] = mask_bank_account(row[6])
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 10, "GOVERNMENT OF PURVANCHAL", ln=1, align="C")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 8, "Department of Social Welfare", ln=1, align="C")
    pdf.cell(0, 8, "Old Age Pension Scheme - Acknowledgment", ln=1, align="C")
    pdf.ln(4)
    # letterhead rules
    pdf.line(10, 12, 200, 12)
    pdf.line(10, 282, 200, 282)
    labels = ["Application No", "Applicant Name", "Mobile", "Date of Birth",
              "Village", "Block", "Bank Account", "IFSC", "Submitted At", "Status"]
    pdf.set_font("Helvetica", "", 10)
    for label, val in zip(labels, row):
        try:
            pdf.cell(60, 8, label, border=1)
            pdf.cell(0, 8, str(val), border=1, ln=1)
        except Exception:
            pdf.cell(0, 8, "?", border=1, ln=1)
    pdf.ln(6)
    pdf.set_font("Helvetica", "I", 9)
    pdf.multi_cell(0, 5, "This is a computer generated acknowledgment. Processing SLA "
                         "as per the Purvanchal Right to Public Services Act applies.")
    return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Status portal (citizen login)
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET", "POST"])
def status_login():
    if request.method == "POST":
        mobile = request.form.get("mobile", "").strip()
        password = request.form.get("password", "")
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT password_hash FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        cur.close(); conn.close()
        if row and row[0] == hash_password(password):
            session["logged_in"] = True
            session["portal_mobile"] = mobile
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT id FROM applications WHERE mobile = %s "
                        "ORDER BY submitted_at DESC LIMIT 1", (mobile,))
            r = cur.fetchone()
            cur.close(); conn.close()
            if r:
                return redirect(url_for("view_application", app_id=r[0]))
            flash("No application found for this mobile number.")
            return redirect(url_for("status_login"))
        flash("Mobile number or password is incorrect. The password is your date of "
              "birth as DDMMYYYY.")
    return render_template("status_login.html")


@app.route("/application/<int:app_id>")
def view_application(app_id):
    if not session.get("logged_in"):
        flash("Please login to view application status.")
        return redirect(url_for("status_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, village, block, "
                "bank_account, ifsc, status, submitted_at, decided_at "
                "FROM applications WHERE id = %s AND mobile = %s",
                (app_id, session.get("portal_mobile")))
    row = cur.fetchone()
    cur.close(); conn.close()
    if not row:
        abort(404)
    return render_template("application.html", a=row, app_id=app_id)


@app.route("/application/<int:app_id>/withdraw", methods=["POST"])
def withdraw_application(app_id):
    """Applicant withdraws their own pending application."""
    if not session.get("logged_in") or not session.get("portal_mobile"):
        flash("Please login to manage your application.")
        return redirect(url_for("status_login"))
    mobile = session["portal_mobile"]
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = 'WITHDRAWN', decided_at = %s, "
                "decided_by = 'APPLICANT' WHERE id = %s AND mobile = %s "
                "AND status = 'PENDING' RETURNING application_no",
                (datetime.now(), app_id, mobile))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, "WITHDRAW", "applicant:%s" % mobile)
        conn.commit()
        flash("Application %s has been withdrawn. You may start a new application "
              "using this verified mobile number."
              % row[0])
    else:
        flash("Only a pending application can be withdrawn.")
    cur.close(); conn.close()
    return redirect(url_for("view_application", app_id=app_id))


@app.route("/ack/<int:app_id>.pdf")
def ack_pdf(app_id):
    if not session.get("admin"):
        conn0 = get_db(); c0 = conn0.cursor()
        c0.execute("SELECT 1 FROM applications WHERE id = %s AND mobile = %s",
                   (app_id, session.get("portal_mobile")))
        owned = c0.fetchone()
        c0.close(); conn0.close()
        if not owned:
            abort(403)
    path = os.path.join(UPLOAD_DIR, "ack", "%d.pdf" % app_id)
    if not os.path.exists(path):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM applications WHERE id = %s", (app_id,))
        if not cur.fetchone():
            cur.close(); conn.close()
            abort(404)
        data = generate_acknowledgment(cur, app_id)
        cur.close(); conn.close()
        return send_file(io.BytesIO(data), mimetype="application/pdf")
    return send_file(path, mimetype="application/pdf")


@app.route("/status/reset", methods=["GET", "POST"])
def reset_password():
    if request.method == "POST":
        mobile = request.form.get("mobile", "")
        conn = get_db()
        cur = conn.cursor()
        # fetch account for reset
        cur.execute("SELECT mobile FROM portal_users WHERE mobile = %s", (mobile,))
        row = cur.fetchone()
        if row:
            cur.execute("SELECT dob FROM applications WHERE mobile = %s LIMIT 1", (mobile,))
            r2 = cur.fetchone()
            if r2:
                newpass = r2[0].strftime("%d%m%Y")
                cur.execute("UPDATE portal_users SET password_hash = %s WHERE mobile = %s",
                            (hash_password(newpass), row[0]))
                conn.commit()
                send_sms(row[0], "Sewa Setu: your password has been reset to your "
                                 "date of birth (DDMMYYYY).")
        cur.close(); conn.close()
        flash("If the mobile number exists, the password has been reset and sent by SMS.")
    return render_template("reset.html")


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if (ADMIN_PASSWORD and request.form.get("username") == ADMIN_USERNAME and
                request.form.get("password") == ADMIN_PASSWORD):
            session["logged_in"] = True
            session["admin"] = True
            session["admin_user"] = ADMIN_USERNAME
            return redirect(url_for("admin_dashboard"))
        flash("Invalid credentials.")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT status, count(*) FROM applications GROUP BY status")
    by_status = cur.fetchall()
    cur.execute("SELECT count(*) FROM applications WHERE status = 'PENDING' "
                "AND submitted_at < %s", (datetime.now() - timedelta(days=SLA_DAYS),))
    overdue = cur.fetchone()[0]
    cur.execute("SELECT block, count(*) FROM applications WHERE status = 'PENDING' "
                "GROUP BY block ORDER BY count(*) DESC")
    by_block = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_dashboard.html", by_status=by_status,
                           overdue=overdue, by_block=by_block, sla=SLA_DAYS)


@app.route("/admin/applications")
def admin_list():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    status = request.args.get("status", "PENDING")
    mobile = request.args.get("mobile", "").strip()
    page = int(request.args.get("page", 1))
    conn = get_db()
    cur = conn.cursor()
    if mobile:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE mobile = %s ORDER BY submitted_at ASC",
                    (mobile,))
        status = "mobile %s" % mobile
    else:
        cur.execute("SELECT id, application_no, applicant_name, mobile, block, status, "
                    "submitted_at FROM applications WHERE status = %s "
                    "ORDER BY submitted_at ASC LIMIT 50 OFFSET %s",
                    (status, (page - 1) * 50))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_list.html", rows=rows, status=status, page=page, mobile=mobile)


@app.route("/admin/application/<int:app_id>")
def admin_view(app_id):
    if not session.get("admin"):
        return redirect(url_for("admin_login"))
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT application_no, applicant_name, mobile, dob, gender, "
                "marital_status, village, block, bank_account, ifsc, doc_path, "
                "status, submitted_at, decided_at, decided_by "
                "FROM applications WHERE id = %s", (app_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        abort(404)
    cur.execute("SELECT at, action, actor, note FROM audit_log "
                "WHERE application_id = %s ORDER BY at ASC", (app_id,))
    audit = cur.fetchall()
    cur.close(); conn.close()
    return render_template("admin_view.html", a=row, app_id=app_id, audit=audit)


def _admin_decide(app_id, new_status, note):
    """Approve/reject a PENDING application, recording who decided and when."""
    if not session.get("admin"):
        abort(403)
    actor = session.get("admin_user", ADMIN_USERNAME)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE applications SET status = %s, decided_at = %s, decided_by = %s "
                "WHERE id = %s AND status = 'PENDING' RETURNING application_no",
                (new_status, datetime.now(), actor, app_id))
    row = cur.fetchone()
    if row:
        write_audit(cur, app_id, new_status, "admin:%s" % actor, note)
        conn.commit()
        flash("Application %s %s." % (row[0], new_status.lower()))
    else:
        flash("Only a pending application can be decided.")
    cur.close(); conn.close()
    return redirect(url_for("admin_view", app_id=app_id))


@app.route("/admin/approve/<int:app_id>", methods=["POST"])
def admin_approve(app_id):
    return _admin_decide(app_id, "APPROVED", request.form.get("note", ""))


@app.route("/admin/reject/<int:app_id>", methods=["POST"])
def admin_reject(app_id):
    return _admin_decide(app_id, "REJECTED", request.form.get("note", ""))


from oauth import oauth as _oauth_blueprint   # noqa: E402  (needs the definitions above)
app.register_blueprint(_oauth_blueprint)
from agent_api import agent_api as _agent_api_blueprint  # noqa: E402
app.register_blueprint(_agent_api_blueprint)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
