#!/usr/bin/env python3
"""Offline test: builds a fake scratch tree mirroring the real pipeline layout
and asserts file-based per-method detection, dedup, and email behaviour."""
import json, os, smtplib, sys, tempfile, time

SBOX = tempfile.mkdtemp(prefix="fcd_test_")
SCRATCH = os.path.join(SBOX, "scratch")
ARCHIVE = os.path.join(SBOX, "scratch_archive")
os.environ["FCD_SCRATCH_ROOTS"] = f"{SCRATCH},{ARCHIVE}"
os.environ["FCD_STATE_DIR"] = os.path.join(SBOX, "state")
os.environ["FCD_DASHBOARD_CSV"] = os.path.join(SBOX, "fcd_dashboard.csv")
os.environ["FCD_SMTP_HOST"] = "bad.relay.invalid,good.relay.invalid"

SENT, RELAYS = [], []
class FakeSMTP:
    def __init__(self, host, *a, **k):
        RELAYS.append(host)
        if host == "bad.relay.invalid":
            raise ConnectionRefusedError("simulated down")
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def send_message(self, m): SENT.append(m["Subject"])
smtplib.SMTP = FakeSMTP

import aggregate_dashboard as agg

def mk(root, name): 
    d = os.path.join(root, name); os.makedirs(d, exist_ok=True); return d
def w(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(text)
def meta(d, mrn, methods=None):
    json.dump({"job_id": os.path.basename(d), "mrn": mrn,
               "patient_name": "DOE^J", "submitter": "EPILEPSYML@MR3",
               "submitted_at": time.time(),
               "methods": methods or agg.METHODS},
              open(os.path.join(d, agg.META_NAME), "w"))

# --- A: all three good -> COMPLETE
d = mk(SCRATCH, "4022001_20100101_aaaa"); meta(d, "4022001")
w(d+"/.meld_graph_done")
w(d+"/output/nnunet/Dataset003_FCD/PredictionTs/x.nii.gz", "data")
w(d+"/output/meld_classifier/output/predictions_reports/SUBJ/report.txt", "ok")
w(d+"/job_nnunet.log","Finished: 2026")
w(d+"/meld_graph_job.log","Touched .meld_graph_done")
w(d+"/meld_classifier_job.log","Pipeline rc=0\n done")

# --- B: real-world meld_classifier failure -> FAILED
d = mk(SCRATCH, "4022002_20100102_bbbb"); meta(d, "4022002")
w(d+"/.meld_graph_done")
w(d+"/output/nnunet/Dataset003_FCD/PredictionTs/y.nii.gz", "data")
w(d+"/job_nnunet.log","Finished: 2026")
w(d+"/meld_graph_job.log","Touched .meld_graph_done")
w(d+"/meld_classifier_job.log","Pipeline rc=0\n\u2717 no prediction output at /scratch/...\n")

# --- C: meld_graph still running (fresh log, no sentinel) -> RUNNING
d = mk(SCRATCH, "4022003_20100103_cccc"); meta(d, "4022003")
w(d+"/meld_graph_job.log","...preprocessing...")  # fresh now

# --- D: nnunet OOM -> FAILED
d = mk(SCRATCH, "4022004_20100104_dddd"); meta(d, "4022004")
w(d+"/job_nnunet.log","torch...\nCUDA out of memory. Tried to allocate...\n")

# --- E: pre-dashboard run in ARCHIVE, no meta, infer MRN -> COMPLETE
d = mk(ARCHIVE, "4019999_20090909_eeee_2026-06-12T20-44-46Z")
w(d+"/.meld_graph_done")
w(d+"/output/nnunet/Dataset003_FCD/PredictionTs/z.nii.gz","data")
w(d+"/output/meld_classifier/output/predictions_reports/S/r.txt","ok")
w(d+"/job_nnunet.log","Finished: 2026")
w(d+"/meld_graph_job.log","Touched .meld_graph_done")
w(d+"/meld_classifier_job.log","ok")

# --- F: SAME job in scratch (live) AND archive -> must appear ONCE
live = mk(SCRATCH, "4022006_20100106_ffff"); meta(live, "4022006")
w(live+"/.meld_graph_done"); w(live+"/job_nnunet.log","Finished: 2026")
w(live+"/output/nnunet/D/PredictionTs/a.nii.gz","d")
w(live+"/output/meld_classifier/output/predictions_reports/S/r.txt","ok")
w(live+"/meld_graph_job.log","x")
arch = mk(ARCHIVE, "4022006_20100106_ffff_2026-06-13T01-00-00Z"); meta(arch,"4022006")
w(arch+"/.meld_graph_done"); w(arch+"/job_nnunet.log","Finished: 2026")
w(arch+"/output/nnunet/D/PredictionTs/a.nii.gz","d")
w(arch+"/output/meld_classifier/output/predictions_reports/S/r.txt","ok")
w(arch+"/meld_graph_job.log","x")

agg.main()
first = sorted(SENT)
agg.main()
import csv
rows = {r["job_id"]: r for r in csv.DictReader(open(os.environ["FCD_DASHBOARD_CSV"]))}

ok = True
def chk(c, label):
    global ok; ok = ok and c
    print(f"  [{'PASS' if c else 'FAIL'}] {label}")

print("per-method + rollup:")
chk(rows["4022001_20100101_aaaa"]["job_status"]=="COMPLETE","A all good -> COMPLETE")
chk(rows["4022002_20100102_bbbb"]["meld_classifier"]=="FAILED","B meld_classifier -> FAILED (no pred output)")
chk(rows["4022002_20100102_bbbb"]["nnunet"]=="DONE","B nnunet -> DONE")
chk(rows["4022002_20100102_bbbb"]["job_status"]=="FAILED","B job -> FAILED")
chk(rows["4022003_20100103_cccc"]["meld_graph"]=="RUNNING","C meld_graph -> RUNNING")
chk(rows["4022003_20100103_cccc"]["job_status"]=="RUNNING","C job -> RUNNING")
chk(rows["4022004_20100104_dddd"]["nnunet"]=="FAILED","D nnunet OOM -> FAILED")
chk(rows["4019999_20090909_eeee"]["job_status"]=="COMPLETE","E pre-dashboard archive -> COMPLETE")
chk(rows["4019999_20090909_eeee"]["mrn"]=="4019999","E MRN inferred from dir name")
chk(rows["4019999_20090909_eeee"]["submitter"]=="(pre-dashboard)","E flagged pre-dashboard")

print("dedup + email:")
chk(len([k for k in rows if k.startswith("4022006")])==1,"F live+archive collapse to one row")
chk(len(first)>=3,f"emails sent first run ({len(first)})")
chk(sorted(SENT)==first,"no duplicate emails on rerun")
chk("good.relay.invalid" in RELAYS,"relay failover fired")

print(f"\nsandbox: {SBOX}\nRESULT:", "ALL PASS" if ok else "FAILURES")
sys.exit(0 if ok else 1)
