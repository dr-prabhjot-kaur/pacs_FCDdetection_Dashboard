#!/usr/bin/env python3
"""Aggregate per-job FCD pipeline status into a CSV and email on completion.

Run from cron (e.g. every 5 min) on a host that can both see STATUS_ROOT and
reach the SMTP relay (typically Rainbow, where OUTPUT_CSV lives in the
OneDrive-synced folder). Writes OUTPUT_CSV atomically. Emails once per job
when it reaches a terminal state (COMPLETE or FAILED); a .notified sentinel
in the job dir guards against re-sending on subsequent cron runs.

PHI (MRN, patient name) is included in the CSV and email by design — all
recipients are internal BCH M365 users and OneDrive is on the M365 tenant.
"""

import csv
import os
import smtplib
import sys
import time
from email.message import EmailMessage

import dashboard_status as ds  # must be on PYTHONPATH / same dir

# ---- EDIT THESE (or set the env vars) -------------------------------------
OUTPUT_CSV = os.environ.get(
    "FCD_DASHBOARD_CSV",
    "/fileserver/Rad-Warfield-e2/CHANGE_ME/OneDrive/fcd_dashboard.csv",
)
SMTP_HOST = os.environ.get("FCD_SMTP_HOST", "CHANGE_ME.childrens.harvard.edu")
SMTP_PORT = int(os.environ.get("FCD_SMTP_PORT", "25"))   # internal relay, no auth
MAIL_FROM = os.environ.get("FCD_MAIL_FROM", "fcd-pipeline@childrens.harvard.edu")
MAIL_TO = os.environ.get(
    "FCD_MAIL_TO", "prabhjot.kaur@childrens.harvard.edu"
).split(",")
# ---------------------------------------------------------------------------

CSV_FIELDS = ["job_id", "mrn", "patient_name", "submitter", "submitted_at",
              *ds.METHODS, "job_status", "last_update"]


def fmt(epoch):
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(epoch)))


def overall_status(meta, statuses):
    """Roll per-method states up to a single job state.

    Only the methods declared in meta['methods'] count, so a job that never
    dispatched (say) nnunet completes correctly instead of waiting forever.
    """
    states = [statuses[m][0] for m in meta.get("methods", ds.METHODS)]
    if "FAILED" in states:
        return "FAILED"
    if states and all(s == "DONE" for s in states):
        return "COMPLETE"
    if "RUNNING" in states:
        return "RUNNING"
    return "PENDING"


def scan():
    """Return (rows_for_csv, jobs_now_terminal)."""
    rows, terminal = [], []
    if not os.path.isdir(ds.STATUS_ROOT):
        return rows, terminal
    for job_id in sorted(os.listdir(ds.STATUS_ROOT)):
        jdir = os.path.join(ds.STATUS_ROOT, job_id)
        if not os.path.isdir(jdir):
            continue
        meta = ds.read_meta(job_id)
        if meta is None:
            continue  # meta.json not written yet — skip until submitter writes it
        statuses = {m: ds.read_status(job_id, m)
                    for m in meta.get("methods", ds.METHODS)}
        job_status = overall_status(meta, statuses)
        last_update = max([s[1] for s in statuses.values()]
                          + [meta.get("submitted_at", 0.0)])
        row = {
            "job_id": job_id,
            "mrn": meta.get("mrn", ""),
            "patient_name": meta.get("patient_name", ""),
            "submitter": meta.get("submitter", ""),
            "submitted_at": fmt(meta.get("submitted_at")),
            "job_status": job_status,
            "last_update": fmt(last_update),
        }
        for m in ds.METHODS:
            row[m] = statuses[m][0] if m in statuses else "--"  # -- = not dispatched
        rows.append(row)
        if job_status in ("COMPLETE", "FAILED"):
            terminal.append((job_id, meta, statuses, job_status))
    return rows, terminal


def write_csv(rows):
    d = os.path.dirname(OUTPUT_CSV)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = OUTPUT_CSV + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, OUTPUT_CSV)


def notify(job_id, meta, statuses, job_status):
    """Email once per job. O_EXCL sentinel makes this race-safe across runs."""
    sentinel = os.path.join(ds.STATUS_ROOT, job_id, ".notified")
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return  # already emailed
    os.close(fd)

    lines = []
    for m in meta.get("methods", ds.METHODS):
        st, ts, msg = statuses[m]
        lines.append(f"{m:18s} {st:8s} {fmt(ts)}  {msg}".rstrip())
    body = (
        f"Job:        {job_id}\n"
        f"MRN:        {meta.get('mrn', '')}\n"
        f"Patient:    {meta.get('patient_name', '')}\n"
        f"Submitter:  {meta.get('submitter', '')}\n"
        f"Submitted:  {fmt(meta.get('submitted_at'))}\n"
        f"Status:     {job_status}\n\n"
        "Per-method:\n  " + "\n  ".join(lines) + "\n"
    )
    msg = EmailMessage()
    msg["Subject"] = (f"[FCD pipeline] {job_status}: "
                      f"MRN {meta.get('mrn', '')} ({job_id})")
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    msg.set_content(body)
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.send_message(msg)
    except Exception as e:
        os.unlink(sentinel)  # roll back so the next cron run retries
        print(f"[warn] email failed for {job_id}: {e}", file=sys.stderr)


def main():
    rows, terminal = scan()
    write_csv(rows)
    for job_id, meta, statuses, job_status in terminal:
        notify(job_id, meta, statuses, job_status)
    print(f"{fmt(time.time())}  jobs={len(rows)}  "
          f"terminal_this_run={len(terminal)}  csv={OUTPUT_CSV}")


if __name__ == "__main__":
    main()
