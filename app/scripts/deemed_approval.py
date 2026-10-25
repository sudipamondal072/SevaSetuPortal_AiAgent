# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.
"""

import os
import sys
import configparser
from datetime import datetime, timedelta

import psycopg2
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config = configparser.ConfigParser()
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
with open(CONFIG_PATH) as fh:
    config.read_file(fh)
config.set("database", "password", os.environ.get("DB_PASSWORD", ""))

SLA_DAYS = config.getint("pension", "sla_days")
SMS_GATEWAY_URL = config.get("app", "sms_gateway_url")
SMS_GATEWAY_TOKEN = os.environ.get("SMS_GATEWAY_TOKEN", "")


def main():
    if not os.environ.get("DB_PASSWORD"):
        raise RuntimeError("DB_PASSWORD must be set")
    conn = psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS application_flags (
               application_id INTEGER PRIMARY KEY REFERENCES applications(id),
               flag_type VARCHAR(50) NOT NULL,
               reason VARCHAR(500) NOT NULL,
               flagged_by VARCHAR(50) NOT NULL,
               flagged_at TIMESTAMP NOT NULL
           )"""
    )
    cutoff = datetime.now() - timedelta(days=SLA_DAYS)
    cur.execute(
        """SELECT id FROM applications
           WHERE status = 'PENDING' AND submitted_at < %s
           ORDER BY submitted_at ASC
           FOR UPDATE SKIP LOCKED""",
        (cutoff,),
    )
    candidate_ids = [row[0] for row in cur.fetchall()]
    rows = []
    if candidate_ids:
        cur.execute(
            "SELECT application_id FROM application_flags "
            "WHERE application_id = ANY(%s)",
            (candidate_ids,),
        )
        flagged_ids = {row[0] for row in cur.fetchall()}
        eligible_ids = [
            application_id for application_id in candidate_ids
            if application_id not in flagged_ids
        ]
        if eligible_ids:
            decided_at = datetime.now()
            cur.execute(
                """UPDATE applications
                   SET status = 'DEEMED_APPROVED', decided_at = %s,
                       decided_by = 'RTPS-AUTO'
                   WHERE id = ANY(%s) AND status = 'PENDING'
                   RETURNING id, application_no, mobile""",
                (decided_at, eligible_ids),
            )
            rows = cur.fetchall()
    for app_id, _, _ in rows:
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                    "VALUES (%s, 'DEEMED_APPROVED', 'system:rtps-cron', "
                    "'not processed within SLA', %s)", (app_id, datetime.now()))
    conn.commit()
    for _, app_no, mobile in rows:
        try:
            requests.post(SMS_GATEWAY_URL + "/api/send", json={
                "to": mobile,
                "text": "Sewa Setu: your pension application %s stands approved "
                        "under the RTPS Act." % app_no},
                headers={"Authorization": "Bearer " + SMS_GATEWAY_TOKEN},
                timeout=5)
        except Exception:
            pass
    print("%s deemed approval: %d applications approved"
          % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), len(rows)))
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
