"""Retry-only driver: re-scan boards that failed in the main production run.

Usage: python3 retry_failed.py
Reads the latest monitor report's boards_failed list, builds a temporary
watchlist with just those companies, and calls monitor.main() against it.
Restores the real watchlist afterwards. Results merge into jobs.json and
push to GitHub via monitor's normal flow.
"""
import json, os, sys, glob

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor

HOME = os.path.expanduser("~")
WL = f"{HOME}/workspace/newgrad-jobs/ats-watchlist.json"
REPORT_DIR = f"{HOME}/workspace/newgrad-jobs/monitor-reports"

reports = sorted(glob.glob(f"{REPORT_DIR}/*.json"))
latest = json.load(open(reports[-1]))
failed = latest.get("boards_failed") or []
# strip any " (ExceptionType)" suffixes
failed_names = {f.split(" (")[0].strip() for f in failed}
print(f"retrying {len(failed_names)} failed boards from {os.path.basename(reports[-1])}")

wl = json.load(open(WL))
subset = [e for e in wl if e.get("company") in failed_names]
missing = failed_names - {e.get("company") for e in subset}
if missing:
    print("not in watchlist (skipped):", sorted(missing))
print(f"scanning {len(subset)} boards")

backup = WL + ".bak"
os.replace(WL, backup)
try:
    json.dump(subset, open(WL, "w"), indent=2)
    monitor.main()
finally:
    os.replace(backup, WL)
    print("watchlist restored")
