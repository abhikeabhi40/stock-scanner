"""
CLOUD SCAN - runs one scan and writes data.json + index.html
Used by GitHub Actions (see .github/workflows/scan.yml). It reuses the
scan logic from dashboard.py, so the online page is identical to the
local one. Angel One details come from environment variables (GitHub
Secrets) - nothing secret is written to the repository.
"""

import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("TZ", "Asia/Kolkata")
try:
    import time
    time.tzset()
except AttributeError:
    pass

import dashboard  # noqa: E402  (importing does NOT start the web server)

dashboard.PUBLISH_ONLINE = False       # we commit the files instead

OLD = {}
try:
    with open(os.path.join(HERE, "data.json")) as fh:
        OLD = json.load(fh)
except Exception:
    OLD = {}

dashboard.scan()

state = dict(dashboard.STATE, targets=dashboard.TARGETS, stop_loss=dashboard.STOP_LOSS,
             hold_days=dashboard.HOLD_DAYS, runner="github-actions")

# NEVER GO BLANK: if this run's section came back empty (a scanner crashed,
# the broker API hiccupped, etc.) but the previous run had real results for
# it, keep showing the previous results for that section instead of wiping
# it - a blank page is worse than slightly-stale data.
LIST_KEYS = ("top10", "both", "results_only", "volume_only", "climbers", "fo")
kept = [k for k in LIST_KEYS if not state.get(k) and OLD.get(k)]
if kept:
    for k in kept:
        state[k] = OLD[k]
    for k in ("mode", "date", "scan_time", "fo_date", "counts"):
        if not state.get(k) and OLD.get(k):
            state[k] = OLD[k]
    state["data_from"] = OLD.get("updated")
    state["checked_at"] = state.get("updated")
    state["updated"] = OLD.get("updated") or state.get("updated")
    note = f"{datetime.now():%H:%M:%S}  kept previous results for: {', '.join(kept)} (this run returned nothing new there)"
    state["log"] = (OLD.get("log", []) + [note])[-30:]
    print(note)

with open(os.path.join(HERE, "data.json"), "w") as fh:
    json.dump(state, fh, default=str)

page = dashboard.HTML.replace("'/data.json'", "'data.json?t=' + Date.now()")
with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as fh:
    fh.write(page)

print(f"data.json written: mode={state['mode']} date={state['date']} "
      f"scan_time={state['scan_time']} top10={len(state['top10'])}")
