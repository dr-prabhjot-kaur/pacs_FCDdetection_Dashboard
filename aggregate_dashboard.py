#!/usr/bin/env python3
"""Aggregate FCD pipeline status into a CSV and email on completion.

Self-contained (no dashboard_status import). Run from cron (e.g. every 5 min)
on a host that can see the scratch roots and reach the SMTP relay.

Per-method status is DERIVED from the files each method leaves in its working
directory -- no edits to the method scripts are required. The only pipeline
change is organize_inputs.py dropping a small dashboard_meta.json (MRN,
submitter, patient_name, methods) into the working dir. Dirs without that file
(pre-dashboard runs) are still listed, with MRN inferred from the dir name.

A working dir moves from scratch/<id>/ to scratch_archive/<id>_<ts>Z/ after the
run; both roots are scanned and jobs are de-duplicated by the stable <id>.

Detection per method (in order):
    1. method's done-marker present (sentinel / output file) -> DONE
    2. no log yet                                            -> PENDING
    3. log shows a hard failure marker                       -> FAILED
    4. log / output recently modified                        -> RUNNING
    5. otherwise (no output, gone quiet)                     -> FAILED (stalled)

PHI (MRN, patient name) is included in the CSV and email by design -- all
recipients are internal BCH M365 users and OneDrive is on the M365 tenant.
"""

import csv
import glob
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage

# ---- EDIT THESE (or set the env vars) -------------------------------------
SCRATCH_ROOTS = os.environ.get(
    "FCD_SCRATCH_ROOTS",
    "/lab-share/Rad-Warfield-e2/Groups/Imp-Recons/prabhjot/work/gits/"
    "pacs_FCDdetection/scratch,"
    "/lab-share/Rad-Warfield-e2/Groups/Imp-Recons/prabhjot/work/gits/"
    "pacs_FCDdetection/scratch_archive",
).split(",")

# Where <job_id>.notified sentinels live (must persist across archive moves).
STATE_DIR = os.environ.get(
    "FCD_STATE_DIR",
    "/lab-share/Rad-Warfield-e2/Groups/Imp-Recons/prabhjot/work/gits/"
    "pacs_FCDdetection/dashboard_state",
)

OUTPUT_CSV = os.environ.get(
    "FCD_DASHBOARD_CSV",
    "/fileserver/Rad-Warfield-e2/CHANGE_ME/OneDrive/fcd_dashboard.csv",
)

SMTP_HOSTS = os.environ.get(
    "FCD_SMTP_HOST",
    "mailsmtp1.childrenshospital.org,mailsmtp2.childrenshospital.org,"
    "mailsmtp3.childrenshospital.org,mailsmtp4.childrenshospital.org,"
    "mailsmtp5.childrenshospital.org,mailsmtp6.childrenshospital.org,"
    "mailsmtp7.childrenshospital.org",
).split(",")
SMTP_PORT = int(os.environ.get("FCD_SMTP_PORT", "25"))
MAIL_FROM = os.environ.get("FCD_MAIL_FROM", "fcd-pipeline@childrens.harvard.edu")
MAIL_TO = os.environ.get(
    "FCD_MAIL_TO", "prabhjot.kaur@childrens.harvard.edu"
).split(",")

# A method with no output and no log activity for this long is treated as
# FAILED/stalled. Keep generous: FreeSurfer / recon-all is slow.
STALE_SECS = int(os.environ.get("FCD_STALE_SECS", str(6 * 3600)))
# ---------------------------------------------------------------------------

META_NAME = "dashboard_meta.json"
METHODS = ["meld_classifier", "meld_graph", "nnunet"]

_GENERIC_FAIL = [
    r"Traceback \(most recent call last\)",
    r"CUDA out of memory",
    r"OUT_OF_MEMORY",
    r"CANCELLED AT",
    r"slurmstepd: error",
    r"Segmentation fault",
    r"cannot enable executable stack",
    r"\bKilled\b",
]

# Per-method probes; all paths are relative to the working dir.
METHOD_PROBES = {
    "nnunet": {
        "log": "job_nnunet.log",
        "done_glob": "output/nnunet/*/PredictionTs/*.nii.gz",
        "done_log_re": r"^Finished:",
        "fail_res": _GENERIC_FAIL,
        "activity_glob": [],
    },
    "meld_graph": {
        "log": "meld_graph_job.log",
        "done_file": ".meld_graph_done",
        "done_log_re": r"Touched .*\.meld_graph_done",
        "fail_res": _GENERIC_FAIL,
        "activity_glob": ["output/meld/logs/*.log",
                          "output/meld/output/fs_outputs/*/scripts/*.log"],
    },
    "meld_classifier": {
        "log": "meld_classifier_job.log",
        "done_glob": "output/meld_classifier/output/predictions_reports/*",
        # u2717 is the heavy ballot X the wrapper prints on missing output.
        "fail_res": [u"\u2717 no prediction output"] + _GENERIC_FAIL,
        "activity_glob": [
            "output/meld_classifier/output/fs_outputs/*/scripts/*.log"],
    },
}

_ARCHIVE_SUFFIX = re.compile(r"_\d{4}-\d{2}-\d{2}T[\d:-]+Z$")


def fmt(epoch):
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(epoch)))


def _read_tail(path, nbytes=65536):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _has_output(workdir, pattern):
    for p in glob.glob(os.path.join(workdir, pattern)):
        try:
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return True
            if os.path.isdir(p) and os.listdir(p):
                return True
        except OSError:
            pass
    return False


def _newest_mtime(workdir, patterns):
    newest = 0.0
    for pat in patterns:
        for p in glob.glob(os.path.join(workdir, pat)):
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
    return newest


def method_status(workdir, method):
    pr = METHOD_PROBES[method]
    if pr.get("done_file") and os.path.exists(
            os.path.join(workdir, pr["done_file"])):
        return "DONE"
    if pr.get("done_glob") and _has_output(workdir, pr["done_glob"]):
        return "DONE"
    text = _read_tail(os.path.join(workdir, pr["log"]))
    if text is None:
        return "PENDING"
    if pr.get("done_log_re") and re.search(pr["done_log_re"], text, re.M):
        return "DONE"
    for rgx in pr["fail_res"]:
        if re.search(rgx, text, re.M):
            return "FAILED"
    activity = [pr["log"]] + pr.get("activity_glob", [])
    if time.time() - _newest_mtime(workdir, activity) < STALE_SECS:
        return "RUNNING"
    return "FAILED"


def _infer_job_id(d):
    return _ARCHIVE_SUFFIX.sub("", os.path.basename(d.rstrip("/")))


def _read_meta(workdir):
    try:
        with open(os.path.join(workdir, META_NAME)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def load_job(workdir):
    """Return identity dict for a working dir, or None if it isn't a job dir."""
    meta = _read_meta(workdir)
    if meta:
        return {
            "job_id": _infer_job_id(workdir),  # dir name is source of truth
            "mrn": str(meta.get("mrn", "")),
            "patient_name": meta.get("patient_name", ""),
            "submitter": meta.get("submitter", ""),
            "submitted_at": float(meta.get("submitted_at", 0.0) or 0.0),
            "methods": meta.get("methods", METHODS),
            "workdir": workdir,
        }
    # pre-dashboard fallback: any method log present -> infer from dir name
    if any(os.path.exists(os.path.join(workdir, METHOD_PROBES[m]["log"]))
           for m in METHODS):
        jid = _infer_job_id(workdir)
        try:
            submitted_at = os.path.getmtime(workdir)
        except OSError:
            submitted_at = 0.0
        return {
            "job_id": jid,
            "mrn": jid.split("_")[0] if jid else "",
            "patient_name": "",
            "submitter": "(pre-dashboard)",
            "submitted_at": submitted_at,
            "methods": METHODS,
            "workdir": workdir,
        }
    return None


def overall_status(methods, statuses):
    states = [statuses[m] for m in methods]
    if "FAILED" in states:
        return "FAILED"
    if states and all(s == "DONE" for s in states):
        return "COMPLETE"
    if "RUNNING" in states:
        return "RUNNING"
    return "PENDING"


def scan():
    """Walk both scratch roots, de-dupe by job_id (keep newest), build rows."""
    best = {}
    for root in SCRATCH_ROOTS:
        root = root.strip()
        if not root or not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            workdir = os.path.join(root, name)
            if not os.path.isdir(workdir):
                continue
            job = load_job(workdir)
            if job is None:
                continue
            statuses = {m: method_status(workdir, m) for m in job["methods"]}
            last_update = max(
                [_newest_mtime(workdir, [METHOD_PROBES[m]["log"]])
                 for m in job["methods"]] + [job["submitted_at"], 0.0])
            rec = {**job, "statuses": statuses,
                   "job_status": overall_status(job["methods"], statuses),
                   "last_update": last_update}
            prev = best.get(job["job_id"])
            if prev is None or last_update >= prev["last_update"]:
                best[rec["job_id"]] = rec

    rows, terminal = [], []
    for rec in sorted(best.values(), key=lambda r: r["last_update"],
                      reverse=True):
        row = {
            "job_id": rec["job_id"],
            "mrn": rec["mrn"],
            "patient_name": rec["patient_name"],
            "submitter": rec["submitter"],
            "submitted_at": fmt(rec["submitted_at"]),
            "job_status": rec["job_status"],
            "last_update": fmt(rec["last_update"]),
        }
        for m in METHODS:
            row[m] = rec["statuses"].get(m, "--")  # -- = method not expected
        rows.append(row)
        if rec["job_status"] in ("COMPLETE", "FAILED"):
            terminal.append(rec)
    return rows, terminal


CSV_FIELDS = ["job_id", "mrn", "patient_name", "submitter", "submitted_at",
              *METHODS, "job_status", "last_update"]


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


def notify(rec):
    """Email once per job, keyed by job_id so archive moves can't double-send."""
    os.makedirs(STATE_DIR, exist_ok=True)
    sentinel = os.path.join(STATE_DIR, f"{rec['job_id']}.notified")
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    os.close(fd)

    lines = [f"{m:18s} {rec['statuses'].get(m, '--')}" for m in rec["methods"]]
    body = (
        f"Job:        {rec['job_id']}\n"
        f"MRN:        {rec['mrn']}\n"
        f"Patient:    {rec['patient_name']}\n"
        f"Submitter:  {rec['submitter']}\n"
        f"Submitted:  {fmt(rec['submitted_at'])}\n"
        f"Status:     {rec['job_status']}\n"
        f"Workdir:    {rec['workdir']}\n\n"
        "Per-method:\n  " + "\n  ".join(lines) + "\n"
    )
    msg = EmailMessage()
    msg["Subject"] = (f"[FCD pipeline] {rec['job_status']}: "
                      f"MRN {rec['mrn']} ({rec['job_id']})")
    msg["From"] = MAIL_FROM
    msg["To"] = ", ".join(MAIL_TO)
    msg.set_content(body)

    sent = False
    for host in SMTP_HOSTS:
        host = host.strip()
        if not host:
            continue
        try:
            with smtplib.SMTP(host, SMTP_PORT, timeout=30) as s:
                s.send_message(msg)
            sent = True
            break
        except Exception as e:
            print(f"[warn] relay {host} failed for {rec['job_id']}: {e}",
                  file=sys.stderr)
    if not sent:
        os.unlink(sentinel)  # all relays down -> retry next run


def main():
    rows, terminal = scan()
    write_csv(rows)
    for rec in terminal:
        notify(rec)
    print(f"{fmt(time.time())}  jobs={len(rows)}  "
          f"terminal={len(terminal)}  csv={OUTPUT_CSV}")


if __name__ == "__main__":
    main()
