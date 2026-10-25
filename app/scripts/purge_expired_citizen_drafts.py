import configparser
import logging
import os
from datetime import datetime, timedelta

import psycopg2


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
config = configparser.ConfigParser()
config.read(os.path.join(BASE_DIR, "config", "app.ini"))
config.set("database", "password", os.environ.get("DB_PASSWORD", ""))
UPLOAD_DIR = config.get("app", "upload_dir")


def main():
    if not os.environ.get("DB_PASSWORD"):
        raise RuntimeError("DB_PASSWORD must be set")
    try:
        retention_days = int(os.getenv("CITIZEN_DRAFT_RETENTION_DAYS", "30"))
    except ValueError as exc:
        raise RuntimeError("CITIZEN_DRAFT_RETENTION_DAYS must be a positive integer") from exc
    if retention_days < 1:
        raise RuntimeError("CITIZEN_DRAFT_RETENTION_DAYS must be a positive integer")

    conn = psycopg2.connect(
        host=config.get("database", "host"),
        port=config.get("database", "port"),
        dbname=config.get("database", "name"),
        user=config.get("database", "user"),
        password=config.get("database", "password"),
    )
    try:
        cur = conn.cursor()
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
        cutoff = datetime.now() - timedelta(days=retention_days)
        cur.execute(
            "SELECT draft_ref, doc_path FROM citizen_application_drafts "
            "WHERE completed = FALSE AND updated_at < %s "
            "FOR UPDATE SKIP LOCKED",
            (cutoff,),
        )
        expired = cur.fetchall()
        draft_refs = [row[0] for row in expired]
        if draft_refs:
            cur.execute(
                "DELETE FROM citizen_draft_reviews WHERE draft_ref = ANY(%s)",
                (draft_refs,),
            )
            cur.execute(
                "DELETE FROM citizen_application_drafts WHERE draft_ref = ANY(%s)",
                (draft_refs,),
            )
        conn.commit()
        upload_root = os.path.realpath(UPLOAD_DIR)
        for _, document_path in expired:
            if not document_path:
                continue
            resolved_path = os.path.realpath(document_path)
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
                    logging.exception("expired citizen draft file cleanup failed")
        print("Expired citizen drafts purged: %d" % len(expired))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
