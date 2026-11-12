"""
MORNING SCANNER - runs BOTH scanners and shows the stocks they agree on
=======================================================================
Runs volume_shocker_scanner.py and results_day_scanner.py (same folder),
then prints:
  1. BOTH   - stocks found by both scanners  (strongest: volume + a reason)
  2. RESULTS ONLY - results stocks reacting, no extreme volume (yet)
  3. VOLUME ONLY  - volume shockers without a results trigger

You can start it at 9:15 AM. The first scan runs at FIRST_SCAN (9:20 by
default - the first few minutes have too little data to judge), and it
then repeats every REPEAT_EVERY_MIN minutes until STOP_AT.
Started later in the day, it simply scans right away.

Outside market hours it runs both scanners in BACKTEST mode on the
TEST_DATE set inside each scanner file.

Nothing is saved to disk. Read-only: places no orders.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta

# BACKTEST (used only when the market is closed): date and time to test.
# TEST_DATE = ""  -> automatic: the most recent trading day
#                    (today after 3:30 PM, otherwise the last trading day before today)
# TEST_DATE = "2026-11-04" -> that specific day
TEST_DATE = ""
SCAN_TIME = "09:45"

# NSE holidays (weekdays with no trading) - skipped by the automatic date
NSE_HOLIDAYS_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
                     "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
                     "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
                     "2026-11-10", "2026-11-24", "2026-12-25"]

FIRST_SCAN = "09:20"         # don't scan before this (too little data at 9:15)
REPEAT_EVERY_MIN = 5         # 0 = scan once and stop
STOP_AT = "10:30"            # stop repeating after this time
TOP_N = 10

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNERS = {
    "VOLUME": os.path.join(HERE, "volume_shocker_scanner.py"),
    "RESULTS": os.path.join(HERE, "results_day_scanner.py"),
}
sys.dont_write_bytecode = True


def hm(t):
    return datetime.strptime(t, "%H:%M").time()


def run_scanner(name, path, show_output):
    """Run one scanner as a separate process; return its result dict."""
    fd, tmp = tempfile.mkstemp(prefix=f"{name}_", suffix=".json")
    os.close(fd)
    env = dict(os.environ, SCANNER_JSON_OUT=tmp, RESULTS_SCANNER_CHILD="1")
    if BACKTEST and TEST_DATE:
        env["SCANNER_TEST_DATE"] = TEST_DATE
        env["SCANNER_SCAN_TIME"] = SCAN_TIME
    print(f"\n----- running {os.path.basename(path)} -----")
    p = subprocess.run([sys.executable, path], env=env, cwd=HERE,
                       capture_output=not show_output, text=True)
    data = {"top": [], "all": []}
    try:
        if os.path.getsize(tmp) > 0:
            with open(tmp) as fh:
                data = json.load(fh)
        else:
            print(f"  {name}: produced no result"
                  + (f" (exit code {p.returncode})" if p.returncode else ""))
            if not show_output:
                tail = ((p.stdout or "") + "\n" + (p.stderr or "")).strip().splitlines()[-6:]
                if tail:
                    print("  last lines:\n    " + "\n    ".join(tail))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return data


def line(m, tag):
    out = (f"  [{m.get('grade', '?')}] {m['symbol']:<13} score {m.get('score', 0):>5.0f} | "
           f"price {m['price']:>9.2f} | chg {m.get('change', 0):+5.1f}% | "
           f"VOL {m.get('vol_pct', 0):4.0f}% of day | {m.get('turnover_cr', 0):5.1f}cr | "
           f"VWAP {'Y' if m.get('above_vwap') else 'N'} | news {m.get('news', '-')}")
    if tag:
        out += f" | {tag}"
    if m.get("fails"):
        out += f"\n         missed: {m['fails']}"
    return out


def plan(m):
    adr = m.get("adr") or 3.0
    r = lambda x: round(x * 2) / 2
    stop = r(min(max(adr * 0.6, 1.5), 4.0))
    tg = [r(min(max(adr * k, 1.5), 20.0)) for k in (0.75, 1.5, 2.5)]
    e = m["price"]
    return (f"  {m['symbol']:<13} entry ~{e:.2f} | stop {e * (1 - stop / 100):.2f} (-{stop:g}%) | "
            + " | ".join(f"T{k} {e * (1 + t / 100):.2f} (+{t:g}%)" for k, t in enumerate(tg, 1)))


def scan(show_output):
    vol = run_scanner("VOLUME", SCANNERS["VOLUME"], show_output)
    res = run_scanner("RESULTS", SCANNERS["RESULTS"], show_output)

    v_all = {m["symbol"]: m for m in vol.get("all", [])}
    r_all = {m["symbol"]: m for m in res.get("all", [])}
    v_top = [m["symbol"] for m in vol.get("top", [])]
    r_top = [m["symbol"] for m in res.get("top", [])]

    both = [s for s in v_all if s in r_all]
    both.sort(key=lambda s: (v_all[s].get("grade") == "A") + (r_all[s].get("grade") == "A"),
              reverse=True)
    both.sort(key=lambda s: v_all[s].get("score", 0) + r_all[s].get("score", 0), reverse=True)
    res_only = [s for s in r_top if s not in v_all][:TOP_N]
    vol_only = [s for s in v_top if s not in r_all][:TOP_N]

    when = vol.get("scan_time") or res.get("scan_time") or datetime.now().strftime("%H:%M")
    date = vol.get("date") or res.get("date") or ""
    print("\n" + "=" * 118)
    print(f"MORNING SCAN at {when} on {date}   "
          f"(volume shockers: {len(v_all)}, results stocks reacting: {len(r_all)})")
    print("=" * 118)

    print(f"\n1. BOTH - heavy volume AND results trigger  ({len(both)})   <-- strongest")
    if not both:
        print("  none yet")
    for s in both[:TOP_N]:
        m = dict(v_all[s]); m["news"] = r_all[s].get("news", m.get("news"))
        print(line(m, f"results {r_all[s].get('results', '')}"))
    if both:
        print("\n  TRADE PLAN (BOTH list)")
        for s in both[:TOP_N]:
            print(plan(v_all[s]))

    print(f"\n2. RESULTS ONLY - reacting to results, volume not extreme yet  ({len(res_only)})")
    if not res_only:
        print("  none")
    for s in res_only:
        print(line(r_all[s], f"results {r_all[s].get('results', '')}"))

    print(f"\n3. VOLUME ONLY - volume shocker, no results trigger  ({len(vol_only)})")
    if not vol_only:
        print("  none")
    for s in vol_only:
        print(line(v_all[s], ""))
    print("\nStocks in list 1 are the best candidates. Lists 2 and 3 are a watchlist.")


# ============================================================
# MAIN
# ============================================================

for name, path in SCANNERS.items():
    if not os.path.exists(path):
        print(f"Missing file: {path}")
        sys.exit(1)

now = datetime.now()
market_day = now.weekday() < 5 and now.strftime("%Y-%m-%d") not in NSE_HOLIDAYS_2026
if now.weekday() < 5 and not market_day:
    print("Today is an NSE holiday - running in BACKTEST mode.")

# Started early on a trading day (before 9:15)? Wait for the market to open.
if market_day and hm("08:00") <= now.time() < hm("09:15"):
    print(f"Market opens at 9:15. Waiting for the first scan at {FIRST_SCAN}...")
    target = datetime.combine(now.date(), hm(FIRST_SCAN))
    while datetime.now() < target:
        time.sleep(5)
    now = datetime.now()

in_market = market_day and hm("09:15") <= now.time() <= hm("15:30")
BACKTEST = not in_market


def last_trading_day():
    """Today if the market has closed today, else the previous trading day."""
    d = now.date()
    if now.time() < hm("15:30"):
        d -= timedelta(days=1)
    while d.weekday() >= 5 or d.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


if BACKTEST and not TEST_DATE:
    TEST_DATE = last_trading_day()
    print(f"Automatic date: {TEST_DATE} (most recent trading day)")

if BACKTEST:
    print("Market is closed - running both scanners in BACKTEST mode for "
          + (f"{TEST_DATE} at {SCAN_TIME}." if TEST_DATE
             else "the dates set inside each scanner file."))
    scan(show_output=False)
    sys.exit(0)

if now.time() < hm(FIRST_SCAN):
    target = datetime.combine(now.date(), hm(FIRST_SCAN))
    print(f"Market just opened. Waiting until {FIRST_SCAN} for the first scan "
          f"(first minutes have too little volume data to judge)...")
    while datetime.now() < target:
        time.sleep(5)

stop_t = hm(STOP_AT)
while True:
    scan(show_output=False)
    if not REPEAT_EVERY_MIN:
        break
    nxt = datetime.now() + timedelta(minutes=REPEAT_EVERY_MIN)
    if nxt.time() >= stop_t or nxt.time() > hm("15:25"):
        print(f"\nStopped (STOP_AT = {STOP_AT}).")
        break
    print(f"\nNext scan at {nxt:%H:%M}  (Ctrl+C to stop)")
    try:
        while datetime.now() < nxt:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopped.")
        break
