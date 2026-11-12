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
dashboard.scan()

state = dict(dashboard.STATE, targets=dashboard.TARGETS, stop_loss=dashboard.STOP_LOSS,
             hold_days=dashboard.HOLD_DAYS, runner="github-actions")
with open(os.path.join(HERE, "data.json"), "w") as fh:
    json.dump(state, fh, default=str)

page = dashboard.HTML.replace("'/data.json'", "'data.json?t=' + Date.now()")
with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as fh:
    fh.write(page)

print(f"data.json written: mode={state['mode']} date={state['date']} "
      f"scan_time={state['scan_time']} top10={len(state['top10'])}")
