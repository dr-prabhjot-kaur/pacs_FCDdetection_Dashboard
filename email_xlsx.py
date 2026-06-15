#!/usr/bin/env python3
"""Email the dashboard XLSX as an attachment, but ONLY when the data changed.

Pairs with a Power Automate flow that watches for a fixed subject and writes the
attachment into SharePoint/OneDrive. Change-detection (md5 of the CSV) avoids
spamming a flow run every cycle when nothing moved.

    python3 email_xlsx.py <xlsx> <csv>
env:
    FCD_UPLOAD_TO     recipient (default: your BCH address)
    FCD_UPLOAD_SUBJECT  fixed subject the flow filters on
    FCD_SMTP_HOST / FCD_SMTP_PORT / FCD_MAIL_FROM   (same relays as aggregator)
    FCD_STATE_DIR     where the last-sent hash is stored
"""
import hashlib
import os
import smtplib
import sys
from email.message import EmailMessage

UPLOAD_TO = os.environ.get("FCD_UPLOAD_TO",
                           "prabhjot.kaur@childrens.harvard.edu").split(",")
SUBJECT = os.environ.get("FCD_UPLOAD_SUBJECT", "FCD-DASHBOARD-UPLOAD")
MAIL_FROM = os.environ.get("FCD_MAIL_FROM", "fcd-pipeline@childrens.harvard.edu")
SMTP_HOSTS = os.environ.get(
    "FCD_SMTP_HOST",
    "mailsmtp1.childrenshospital.org,mailsmtp2.childrenshospital.org,"
    "mailsmtp3.childrenshospital.org,mailsmtp4.childrenshospital.org,"
    "mailsmtp5.childrenshospital.org,mailsmtp6.childrenshospital.org,"
    "mailsmtp7.childrenshospital.org",
).split(",")
SMTP_PORT = int(os.environ.get("FCD_SMTP_PORT", "25"))
STATE_DIR = os.environ.get("FCD_STATE_DIR", ".")


def _csv_hash(csv_path):
    h = hashlib.md5()
    with open(csv_path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _send(xlsx_path):
    msg = EmailMessage()
    msg["Subject"] = SUBJECT
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(UPLOAD_TO)
    msg.set_content("Automated FCD dashboard upload. Attachment is the current "
                    "dashboard as XLSX.")
    with open(xlsx_path, "rb") as f:
        data = f.read()
    msg.add_attachment(
        data, maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=os.path.basename(xlsx_path))
    for host in SMTP_HOSTS:
        host = host.strip()
        if not host:
            continue
        try:
            with smtplib.SMTP(host, SMTP_PORT, timeout=30) as s:
                s.send_message(msg)
            return True
        except Exception as e:
            print(f"[warn] relay {host} failed: {e}", file=sys.stderr)
    return False


def main():
    xlsx_path = sys.argv[1] if len(sys.argv) > 1 else "fcd_dashboard.xlsx"
    csv_path = sys.argv[2] if len(sys.argv) > 2 else "fcd_dashboard.csv"

    cur = _csv_hash(csv_path)
    os.makedirs(STATE_DIR, exist_ok=True)
    hash_file = os.path.join(STATE_DIR, ".upload_hash")
    prev = ""
    if os.path.exists(hash_file):
        with open(hash_file) as f:
            prev = f.read().strip()

    if cur == prev:
        print("no change since last upload; not sending")
        return

    if _send(xlsx_path):
        with open(hash_file, "w") as f:
            f.write(cur)
        print(f"sent {xlsx_path} to {', '.join(UPLOAD_TO)}")
    else:
        print("[warn] all relays failed; will retry next cycle", file=sys.stderr)


if __name__ == "__main__":
    main()
