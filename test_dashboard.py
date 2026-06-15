#!/usr/bin/env python3
"""Offline test for the FCD dashboard. No SLURM, no real SMTP, no real DICOM.

Sandboxes STATUS_ROOT + CSV in a temp dir, stubs the mail server, seeds jobs
in every state, runs the aggregator twice, and asserts the results.

    python3 test_dashboard.py
"""
import os
import smtplib
import sys
import tempfile

SBOX = tempfile.mkdtemp(prefix="fcd_test_")
os.environ["FCD_STATUS_ROOT"] = os.path.join(SBOX, "status")
os.environ["FCD_DASHBOARD_CSV"] = os.path.join(SBOX, "fcd_dashboard.csv")
os.environ["FCD_SMTP_HOST"] = "stub"  # never contacted; SMTP is monkeypatched

os.environ["FCD_SMTP_HOST"] = "bad.relay.invalid,good.relay.invalid"  # 1st fails, 2nd works

import dashboard_status as ds          # noqa: E402  (env must be set first)

# ---- capture emails instead of sending --------------------------------
SENT = []
RELAY_ATTEMPTS = []


class FakeSMTP:
    def __init__(self, host, *a, **k):
        RELAY_ATTEMPTS.append(host)
        if host == "bad.relay.invalid":
            raise ConnectionRefusedError("simulated relay down")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def send_message(self, msg): SENT.append(msg["Subject"])


smtplib.SMTP = FakeSMTP
import aggregate_dashboard as agg       # noqa: E402

# ---- seed jobs ---------------------------------------------------------
# A: all three methods, one fails  -> FAILED
ds.write_meta("A_fail", mrn="1000001", submitter="EPILEPSYML@MR3",
              patient_name="DOE^JANE")
ds.set_status("A_fail", "meld_classifier", "DONE")
ds.set_status("A_fail", "meld_graph", "DONE")
ds.set_status("A_fail", "nnunet", "FAILED", "CUDA OOM")

# B: only two methods declared, both done -> COMPLETE (nnunet shows '--')
ds.write_meta("B_done", mrn="1000002", submitter="EPILEPSYML@MR3",
              methods=["meld_classifier", "meld_graph"])
ds.set_status("B_done", "meld_classifier", "DONE")
ds.set_status("B_done", "meld_graph", "DONE")

# C: mid-flight -> RUNNING, no email
ds.write_meta("C_run", mrn="1000003", submitter="EPILEPSYML@MR3")
ds.set_status("C_run", "meld_graph", "RUNNING")

# D: just submitted, nothing started -> PENDING, no email
ds.write_meta("D_pend", mrn="1000004", submitter="EPILEPSYML@MR3")

# ---- run + assert ------------------------------------------------------
agg.main()
first = sorted(SENT)
agg.main()  # rerun: sentinel must suppress repeats
second = sorted(SENT)

import csv
with open(os.environ["FCD_DASHBOARD_CSV"]) as f:
    rows = {r["job_id"]: r for r in csv.DictReader(f)}

ok = True


def check(cond, label):
    global ok
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    ok = ok and cond


print("status rollup:")
check(rows["A_fail"]["job_status"] == "FAILED", "A_fail -> FAILED")
check(rows["B_done"]["job_status"] == "COMPLETE", "B_done -> COMPLETE")
check(rows["B_done"]["nnunet"] == "--", "B_done nnunet shows -- (not dispatched)")
check(rows["C_run"]["job_status"] == "RUNNING", "C_run -> RUNNING")
check(rows["D_pend"]["job_status"] == "PENDING", "D_pend -> PENDING")
check(rows["A_fail"]["mrn"] == "1000001", "MRN recorded")
check(rows["A_fail"]["submitter"] == "EPILEPSYML@MR3", "submitter recorded")

print("email behaviour:")
check(len(first) == 2, f"2 emails on first run (got {len(first)})")
check(any("FAILED" in s for s in first), "FAILED email sent")
check(any("COMPLETE" in s for s in first), "COMPLETE email sent")
check(len(second) == len(first), "no duplicate emails on rerun")

print("relay failover:")
check("bad.relay.invalid" in RELAY_ATTEMPTS, "tried first relay (failed)")
check("good.relay.invalid" in RELAY_ATTEMPTS, "fell over to second relay")

print(f"\nsandbox: {SBOX}")
print("RESULT:", "ALL PASS" if ok else "FAILURES ABOVE")
sys.exit(0 if ok else 1)
