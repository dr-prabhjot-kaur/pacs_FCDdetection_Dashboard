#!/usr/bin/env python3
"""Shared status/metadata helpers for the FCD PACS dashboard.

Lock-free, NFS-safe contract. One directory per job under STATUS_ROOT:

    <STATUS_ROOT>/<job_id>/
        meta.json              # written once at submission (MRN, submitter, ...)
        meld_classifier.status
        meld_graph.status
        nnunet.status
        .notified              # sentinel, created after email is sent

Status file = single line:  STATE<TAB>epoch<TAB>message
    STATE in {PENDING, RUNNING, DONE, FAILED}
All writes are temp-file + atomic rename within the same dir (NFS-safe).
Timestamps use time.time() (NOT datetime.utcnow) to avoid EDT tz bugs.
"""

import json
import os
import sys
import tempfile
import time

# ---- EDIT THESE (or set the env vars) -------------------------------------
# HPC view of the shared filesystem. On Rainbow this same tree is /fileserver/...
STATUS_ROOT = os.environ.get(
    "FCD_STATUS_ROOT",
    "/lab-share/Rad-Warfield-e2/CHANGE_ME/dashboard_status",
)
# ---------------------------------------------------------------------------

METHODS = ["meld_classifier", "meld_graph", "nnunet"]
TERMINAL_STATES = {"DONE", "FAILED"}


def _job_dir(job_id):
    d = os.path.join(STATUS_ROOT, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def _atomic_write(path, text):
    """Write text to path via temp file + os.replace (atomic on same fs)."""
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_meta(job_id, mrn, submitter, patient_name="", accession="",
               methods=None, **extra):
    """Call ONCE at submission time (e.g. from organizeinputs.py / submitter.py).

    Records who/what submitted and the MRN, declares which methods to expect,
    and seeds a PENDING status for each so a job that skips a method (e.g.
    nnunet not dispatched) is never reported as stuck.
    """
    meta = {
        "job_id": job_id,
        "mrn": str(mrn),
        "patient_name": patient_name,
        "accession": accession,
        "submitter": submitter,
        "submitted_at": time.time(),
        "methods": methods if methods is not None else list(METHODS),
    }
    meta.update(extra)
    _atomic_write(os.path.join(_job_dir(job_id), "meta.json"),
                  json.dumps(meta, indent=2))
    for m in meta["methods"]:
        p = os.path.join(_job_dir(job_id), f"{m}.status")
        if not os.path.exists(p):
            set_status(job_id, m, "PENDING")
    return meta


def set_status(job_id, method, state, message=""):
    """Update one method's status. Safe to call from bash via the CLI below."""
    state = state.upper()
    message = message.replace("\t", " ").replace("\n", " ")
    line = f"{state}\t{time.time():.0f}\t{message}\n"
    _atomic_write(os.path.join(_job_dir(job_id), f"{method}.status"), line)


def read_status(job_id, method):
    """Return (state, epoch, message). state == 'MISSING' if no file yet."""
    p = os.path.join(STATUS_ROOT, job_id, f"{method}.status")
    try:
        with open(p) as f:
            parts = f.readline().rstrip("\n").split("\t")
    except FileNotFoundError:
        return ("MISSING", 0.0, "")
    state = parts[0] if parts else "MISSING"
    ts = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
    msg = parts[2] if len(parts) > 2 else ""
    return (state, ts, msg)


def read_meta(job_id):
    p = os.path.join(STATUS_ROOT, job_id, "meta.json")
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# CLI so bash job scripts can update status without writing Python.
#   python3 dashboard_status.py set <job_id> <method> <state> [message]
if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "set":
        set_status(sys.argv[2], sys.argv[3], sys.argv[4],
                   sys.argv[5] if len(sys.argv) > 5 else "")
    else:
        sys.exit("usage: dashboard_status.py set <job_id> <method> "
                 "<PENDING|RUNNING|DONE|FAILED> [message]")
