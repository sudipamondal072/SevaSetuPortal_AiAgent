"""PurvSMS - simulated state SMS gateway.

Stands in for the telecom SMS provider. Messages sent via the API appear on the
web console at / once "delivered", after a short carrier latency.
"""

import hmac
import os
import random
import time
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)
SMS_GATEWAY_TOKEN = os.environ.get("SMS_GATEWAY_TOKEN", "")


def authorized_gateway_request():
    if len(SMS_GATEWAY_TOKEN) < 32:
        return False, 503
    expected = "Bearer " + SMS_GATEWAY_TOKEN
    return hmac.compare_digest(request.headers.get("Authorization", ""), expected), 401

MESSAGES = []  # {id, to, text, sent_at, delivery_at}

# Carrier queue latency, seconds.
DELAY_MIN = 1
DELAY_MAX = 4

CONSOLE = """
<!doctype html><html><head><title>PurvSMS Console</title>
<meta http-equiv="refresh" content="5">
<style>
body{font-family:Arial,sans-serif;margin:24px;background:#f4f4f4}
h2{color:#1a3c6e} table{border-collapse:collapse;background:#fff;width:100%}
td,th{border:1px solid #ccc;padding:6px 10px;font-size:14px;text-align:left}
.q{color:#b36b00}.d{color:#1b7e3c}
</style></head><body>
<h2>PurvSMS Gateway Console (simulation)</h2>
<p>Messages appear below once the carrier delivers them. Auto-refreshes every 5s.</p>
<table><tr><th>#</th><th>To</th><th>Message</th><th>Sent</th><th>Status</th></tr>
{% for m in messages %}
<tr><td>{{m.id}}</td><td>{{m.to}}</td>
<td>{% if m.delivered %}{{m.text}}{% else %}<i>(in transit)</i>{% endif %}</td>
<td>{{m.sent_hhmm}}</td>
<td>{% if m.delivered %}<span class="d">Delivered</span>
{% else %}<span class="q">Queued &middot; ~{{m.eta}}s</span>{% endif %}</td></tr>
{% endfor %}
</table></body></html>
"""

PUBLIC_CONSOLE = """
<!doctype html><html><head><title>PurvSMS Gateway Status</title>
<meta http-equiv="refresh" content="5">
<style>
body{font-family:Arial,sans-serif;margin:24px;background:#f4f4f4}
h2{color:#1a3c6e} table{border-collapse:collapse;background:#fff}
td,th{border:1px solid #ccc;padding:8px 12px;text-align:left}
</style></head><body>
<h2>PurvSMS Gateway Status (simulation)</h2>
<p>Operational message counts only. Recipient numbers and message contents
are available only to authorized administrators.</p>
<table><tr><th>Messages</th><th>In transit</th><th>Delivered</th></tr>
<tr><td>{{ total }}</td><td>{{ queued }}</td><td>{{ delivered }}</td></tr>
</table></body></html>
"""


@app.route("/api/send", methods=["POST"])
def send():
    authorized, status = authorized_gateway_request()
    if not authorized:
        return jsonify({"error": "unauthorized"}), status
    data = request.get_json(force=True, silent=True) or request.form
    to = data.get("to", "")
    text = data.get("text", "")
    if not to or not text:
        return jsonify({"status": "error", "reason": "to and text required"}), 400
    now = time.time()
    msg = {
        "id": len(MESSAGES) + 1,
        "to": to,
        "text": text,
        "sent_at": now,
        "delivery_at": now + random.randint(DELAY_MIN, DELAY_MAX),
    }
    MESSAGES.append(msg)
    return jsonify({"status": "queued", "id": msg["id"]})


@app.route("/api/messages")
def messages():
    authorized, status = authorized_gateway_request()
    if not authorized:
        return jsonify({"error": "unauthorized"}), status
    # Inbox query for a given number. Used to read delivered OTPs/SMS.
    to = request.args.get("to", "")
    now = time.time()
    out = []
    for m in MESSAGES:
        if to and m["to"] != to:
            continue
        out.append({
            "id": m["id"], "to": m["to"],
            "text": m["text"] if now >= m["delivery_at"] else None,
            "delivered": now >= m["delivery_at"],
            "sent_at": m["sent_at"], "delivery_at": m["delivery_at"],
        })
    return jsonify({"messages": out})


@app.route("/")
def console():
    authorized, status = authorized_gateway_request()
    if not authorized:
        return jsonify({"error": "unauthorized"}), status
    now = time.time()
    view = []
    for m in reversed(MESSAGES[-100:]):
        view.append({
            "id": m["id"], "to": m["to"], "text": m["text"],
            "sent_hhmm": time.strftime("%H:%M:%S", time.localtime(m["sent_at"])),
            "delivered": now >= m["delivery_at"],
            "eta": max(0, int(m["delivery_at"] - now)),
        })
    return render_template_string(CONSOLE, messages=view)


@app.route("/public")
def public_console():
    now = time.time()
    total = len(MESSAGES)
    delivered = sum(now >= message["delivery_at"] for message in MESSAGES)
    return render_template_string(
        PUBLIC_CONSOLE,
        total=total,
        queued=total - delivered,
        delivered=delivered,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8025)
