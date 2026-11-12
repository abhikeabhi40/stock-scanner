"""
SCANNER DASHBOARD - runs the scanners automatically and shows results
in your browser at  http://localhost:8000
=====================================================================
Start it once (e.g. at 9:10 AM):   py dashboard.py
  * On a trading day it scans every REFRESH_MIN minutes from 09:15 to
    STOP_AT, using volume_shocker_scanner.py + results_day_scanner.py.
  * Outside market hours it runs one BACKTEST on the most recent trading
    day so the page always has something to show.
  * The web page refreshes itself every 60 seconds.
  * Nothing is saved to disk - results live in memory while this runs.
Press Ctrl+C in the PowerShell window to stop.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8000
REFRESH_MIN = 5              # minutes between scans during market hours
START_AT = "09:15"
STOP_AT = "15:15"            # keep scanning until this time
TOP_N = 10
TARGETS = [5.0, 10.0, 20.0]
STOP_LOSS = 3.0
HOLD_DAYS = 3
OPEN_BROWSER = True

# ---- ONLINE PUBLISHING (optional) ----
# Add these two lines to angel_config.py to publish results online:
#   GITHUB_TOKEN = "github_pat_..."          (fine-grained token, Contents: read/write)
#   GITHUB_REPO  = "yourname/stock-scanner"   (an empty repo you created)
# After each scan, data.json (+ index.html once) is uploaded to that repo.
# Vercel / GitHub Pages then serves it as a website. No login details are uploaded.
PUBLISH_ONLINE = True

HERE = os.path.dirname(os.path.abspath(__file__))
SCANNERS = {"VOLUME": os.path.join(HERE, "volume_shocker_scanner.py"),
            "RESULTS": os.path.join(HERE, "results_day_scanner.py")}
NSE_HOLIDAYS_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
                     "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
                     "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
                     "2026-11-10", "2026-11-24", "2026-12-25"]
sys.dont_write_bytecode = True

STATE = {"status": "starting", "updated": None, "next": None, "mode": "", "date": "",
         "scan_time": "", "top10": [], "both": [], "results_only": [], "volume_only": [],
         "log": [], "counts": {}}
LOCK = threading.Lock()


def hm(t):
    return datetime.strptime(t, "%H:%M").time()


def is_trading_day(d):
    return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in NSE_HOLIDAYS_2026


def log(msg):
    line = f"{datetime.now():%H:%M:%S}  {msg}"
    print(line)
    with LOCK:
        STATE["log"] = (STATE["log"] + [line])[-30:]


# ============================================================
# RUN SCANNERS
# ============================================================

LIVE_OUTPUT = os.environ.get("GITHUB_ACTIONS") == "true"


def run_scanner(name, path):
    fd, tmp = tempfile.mkstemp(prefix=f"{name}_", suffix=".json")
    os.close(fd)
    env = dict(os.environ, SCANNER_JSON_OUT=tmp, RESULTS_SCANNER_CHILD="1",
               PYTHONUNBUFFERED="1")
    if LIVE_OUTPUT:
        # Stream the child's prints straight through so GitHub Actions logs
        # show live progress instead of nothing until it finishes/times out.
        print(f"\n----- {name}: {os.path.basename(path)} -----", flush=True)
        p = subprocess.run([sys.executable, "-u", path], env=env, cwd=HERE, text=True)
    else:
        p = subprocess.run([sys.executable, path], env=env, cwd=HERE,
                           capture_output=True, text=True)
    data = {"top": [], "all": []}
    try:
        if os.path.getsize(tmp) > 0:
            with open(tmp) as fh:
                data = json.load(fh)
        elif not LIVE_OUTPUT:
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-2:]
            log(f"{name}: no result. " + " | ".join(tail))
        else:
            log(f"{name}: no result (exit code {p.returncode})")
    except Exception as err:
        log(f"{name}: could not read result ({err})")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return data


def plan(m):
    e = m["price"]
    return {"stop": round(e * (1 - STOP_LOSS / 100), 2),
            "targets": [round(e * (1 + t / 100), 2) for t in TARGETS]}


def slim(m, source, results=""):
    keys = ("symbol", "grade", "score", "price", "change", "gap", "vol_pct", "turnover_cr",
            "above_vwap", "breakout_20d", "band", "news", "fails", "max_gain", "exit_gain",
            "days_data", "hit_1", "hit_2", "hit_3", "sl_hit_first")
    out = {k: m.get(k) for k in keys}
    out["source"] = source
    out["results"] = results or m.get("results", "")
    out["plan"] = plan(m)
    return out


def scan():
    with LOCK:
        STATE["status"] = "scanning..."
    t0 = time.time()
    vol = run_scanner("VOLUME", SCANNERS["VOLUME"])
    res = run_scanner("RESULTS", SCANNERS["RESULTS"])
    v_all = {m["symbol"]: m for m in vol.get("all", [])}
    r_all = {m["symbol"]: m for m in res.get("all", [])}
    both = [s for s in v_all if s in r_all]
    combined = []
    for s in both:
        m = dict(v_all[s]); m["news"] = r_all[s].get("news", m.get("news"))
        combined.append((v_all[s].get("score", 0) + r_all[s].get("score", 0) + 100,
                         slim(m, "BOTH", r_all[s].get("results", ""))))
    for s, m in r_all.items():
        if s not in both:
            combined.append((m.get("score", 0) + 15, slim(m, "RESULTS")))
    for s, m in v_all.items():
        if s not in both:
            combined.append((m.get("score", 0), slim(m, "VOLUME")))
    combined.sort(key=lambda x: x[0], reverse=True)
    v_top = [m["symbol"] for m in vol.get("top", [])]
    r_top = [m["symbol"] for m in res.get("top", [])]
    with LOCK:
        STATE.update({
            "status": "ok", "updated": datetime.now().strftime("%d-%b %H:%M:%S"),
            "mode": vol.get("mode") or res.get("mode") or "",
            "date": vol.get("date") or res.get("date") or "",
            "scan_time": vol.get("scan_time") or res.get("scan_time") or "",
            "top10": [m for _, m in combined[:TOP_N]],
            "both": [m for _, m in combined if m["source"] == "BOTH"][:TOP_N],
            "results_only": [slim(r_all[s], "RESULTS") for s in r_top if s not in both][:TOP_N],
            "volume_only": [slim(v_all[s], "VOLUME") for s in v_top if s not in both][:TOP_N],
            "counts": {"volume": len(v_all), "results": len(r_all), "both": len(both)},
        })
    log(f"scan done in {time.time() - t0:.0f}s: {len(v_all)} volume, {len(r_all)} results, "
        f"{len(both)} both")
    publish()


# ============================================================
# PUBLISH TO GITHUB (optional)
# ============================================================

def _github_cfg():
    try:
        sys.path.insert(0, HERE)
        import angel_config as ac
        repo = str(getattr(ac, "GITHUB_REPO", "")).strip()
        # accept "owner/repo", a full github.com URL, or a .git URL
        repo = repo.replace("https://", "").replace("http://", "")
        repo = repo.replace("www.github.com/", "").replace("github.com/", "")
        repo = repo.rstrip("/").removesuffix(".git")
        return str(getattr(ac, "GITHUB_TOKEN", "")).strip(), repo
    except ImportError:
        return "", ""


def github_put(token, repo, path, content_bytes, message):
    import base64
    import requests
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "User-Agent": "stock-scanner-dashboard"}
    sha = None
    r = requests.get(url, headers=hdr, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
        if base64.b64decode(r.json().get("content", "").encode()) == content_bytes:
            return "unchanged"
    body = {"message": message, "content": base64.b64encode(content_bytes).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(url, headers=hdr, json=body, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:120]}")
    return "updated"


_page_uploaded = [False]


def publish():
    token, repo = _github_cfg()
    if not (PUBLISH_ONLINE and token and repo):
        return
    try:
        if not _page_uploaded[0]:
            page = HTML.replace("'/data.json'", "'data.json?t=' + Date.now()").encode()
            github_put(token, repo, "index.html", page, "dashboard page")
            _page_uploaded[0] = True
        with LOCK:
            data = json.dumps(dict(STATE, targets=TARGETS, stop_loss=STOP_LOSS,
                                   hold_days=HOLD_DAYS), default=str).encode()
        res = github_put(token, repo, "data.json", data, f"scan {STATE.get('updated')}")
        log(f"online: data.json {res} -> github.com/{repo}")
    except Exception as err:
        log(f"online publish failed: {err}")


def scheduler():
    while True:
        now = datetime.now()
        trading = is_trading_day(now.date())
        if trading and hm(START_AT) <= now.time() <= hm(STOP_AT):
            scan()
            nxt = datetime.now() + timedelta(minutes=REFRESH_MIN)
        else:
            if STATE["updated"] is None or STATE["mode"] == "LIVE":
                log("market closed - running one backtest on the most recent trading day")
                scan()
            # sleep until next trading day's START_AT
            d = now.date() if now.time() < hm(START_AT) else now.date() + timedelta(days=1)
            while not is_trading_day(d):
                d += timedelta(days=1)
            nxt = datetime.combine(d, hm(START_AT))
        with LOCK:
            STATE["next"] = nxt.strftime("%d-%b %H:%M")
        log(f"next scan at {nxt:%d-%b %H:%M}")
        while datetime.now() < nxt:
            time.sleep(5)


# ============================================================
# WEB PAGE
# ============================================================

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Stock Scanner</title>
<meta http-equiv="refresh" content="60">
<style>
 body{font-family:Segoe UI,Arial,sans-serif;background:#0f1419;color:#e6e6e6;margin:0;padding:16px}
 h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:24px 0 8px;color:#9fd3ff}
 .meta{color:#9aa;font-size:13px;margin-bottom:12px}
 table{border-collapse:collapse;width:100%;font-size:13px}
 th,td{padding:6px 8px;border-bottom:1px solid #223;text-align:right;white-space:nowrap}
 th{color:#9aa;font-weight:600;background:#151c24;position:sticky;top:0}
 td:first-child,th:first-child,td.l,th.l{text-align:left}
 tr:hover{background:#16202a}
 .A{color:#4ade80;font-weight:700}.B{color:#facc15;font-weight:700}
 .BOTH{background:#1b3a2a;color:#4ade80;padding:1px 6px;border-radius:4px;font-size:11px}
 .RESULTS{background:#1e2f4a;color:#9fd3ff;padding:1px 6px;border-radius:4px;font-size:11px}
 .VOLUME{background:#3a2a1b;color:#fbbf24;padding:1px 6px;border-radius:4px;font-size:11px}
 .pos{color:#4ade80}.neg{color:#f87171}.dim{color:#778;font-size:11px}
 .yes{color:#4ade80}.no{color:#667}
 .warn{background:#2a1f10;border:1px solid #5a4010;color:#fbbf24;padding:8px 12px;border-radius:6px;font-size:13px;margin:10px 0}
 pre{background:#0b0f14;color:#9aa;font-size:11px;padding:8px;border-radius:6px;max-height:160px;overflow:auto}
</style></head><body>
<h1>Stock Scanner <span id="status" class="dim"></span></h1>
<div class="meta" id="meta"></div>
<div class="warn">Targets T1/T2/T3 are price levels, not predictions. Most picks stop between +3% and +8%.
Always use the stop-loss. This is a research tool, not financial advice.</div>
<h2 id="h_top"></h2><div id="top"></div>
<h2 id="h_both"></h2><div id="both"></div>
<h2 id="h_res"></h2><div id="res"></div>
<h2 id="h_vol"></h2><div id="vol"></div>
<h2>Log</h2><pre id="log"></pre>
<script>
const pct=(v,d=1)=>v==null?'-':((v>0?'+':'')+v.toFixed(d)+'%');
const cls=v=>v==null?'':(v>=0?'pos':'neg');
function table(rows,backtest){
 if(!rows.length) return '<div class="dim">none</div>';
 let h='<table><tr><th>#</th><th class="l">Stock</th><th>Grade</th><th class="l">Source</th><th>Score</th><th>Price</th><th>Chg</th><th>Vol % of day</th><th>Turnover</th><th>VWAP</th><th class="l">News</th><th>Stop -'+STOP+'%</th>';
 TG.forEach((t,i)=>h+='<th>T'+(i+1)+' +'+t+'%</th>');
 if(backtest) h+='<th>Max in '+HOLD+'d</th><th>Exit</th><th>T1</th><th>T2</th><th>T3</th><th>Stop hit</th>';
 h+='<th class="l">Missed</th></tr>';
 rows.forEach((m,i)=>{
  h+='<tr><td>'+(i+1)+'</td><td class="l"><b>'+m.symbol+'</b>'+(m.results?'<div class="dim">'+m.results+'</div>':'')+'</td>'
   +'<td class="'+m.grade+'">'+m.grade+'</td><td class="l"><span class="'+m.source+'">'+m.source+'</span></td>'
   +'<td>'+Math.round(m.score)+'</td><td>'+m.price.toFixed(2)+'</td><td class="'+cls(m.change)+'">'+pct(m.change)+'</td>'
   +'<td>'+Math.round(m.vol_pct)+'%</td><td>'+m.turnover_cr.toFixed(1)+'cr</td><td>'+(m.above_vwap?'Y':'N')+'</td>'
   +'<td class="l">'+(m.news||'-')+'</td><td>'+m.plan.stop.toFixed(2)+'</td>';
  m.plan.targets.forEach(t=>h+='<td>'+t.toFixed(2)+'</td>');
  if(backtest){
   h+='<td class="'+cls(m.max_gain)+'">'+pct(m.max_gain,2)+'</td><td class="'+cls(m.exit_gain)+'">'+pct(m.exit_gain,2)+'</td>';
   [1,2,3].forEach(k=>h+='<td class="'+(m['hit_'+k]?'yes':'no')+'">'+(m['hit_'+k]?'YES':'no')+'</td>');
   h+='<td class="'+(m.sl_hit_first?'neg':'dim')+'">'+(m.sl_hit_first?'YES':'no')+'</td>';
  }
  h+='<td class="l dim">'+(m.fails||'')+'</td></tr>';
 });
 return h+'</table>';
}
let STOP=3,TG=[5,10,20],HOLD=3;
fetch('/data.json').then(r=>r.json()).then(d=>{
 STOP=d.stop_loss;TG=d.targets;HOLD=d.hold_days;
 const bt=d.mode==='BACKTEST'&&d.top10.some(m=>m.max_gain!=null);
 document.getElementById('status').textContent=d.status;
 document.getElementById('meta').innerHTML='Mode: <b>'+d.mode+'</b> &nbsp; Scan: <b>'+d.date+' '+d.scan_time+'</b> &nbsp; Updated: '+(d.updated||'-')+' &nbsp; Next scan: '+(d.next||'-')+' &nbsp; (page refreshes every 60s)';
 document.getElementById('h_top').textContent='TOP '+d.top10.length+' STOCKS (hold up to '+HOLD+' days)';
 document.getElementById('top').innerHTML=table(d.top10,bt);
 document.getElementById('h_both').textContent='1. BOTH - heavy volume AND results trigger ('+d.counts.both+')  <- strongest';
 document.getElementById('both').innerHTML=table(d.both,bt);
 document.getElementById('h_res').textContent='2. RESULTS ONLY ('+d.results_only.length+')';
 document.getElementById('res').innerHTML=table(d.results_only,bt);
 document.getElementById('h_vol').textContent='3. VOLUME ONLY ('+d.volume_only.length+')';
 document.getElementById('vol').innerHTML=table(d.volume_only,bt);
 document.getElementById('log').textContent=d.log.slice().reverse().join('\\n');
});
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/data.json"):
            with LOCK:
                body = json.dumps(dict(STATE, targets=TARGETS, stop_loss=STOP_LOSS,
                                       hold_days=HOLD_DAYS), default=str).encode()
            ctype = "application/json"
        else:
            body = HTML.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    for name, path in SCANNERS.items():
        if not os.path.exists(path):
            print("Missing:", path)
            sys.exit(1)
    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard running at {url}   (Ctrl+C to stop)")
    if OPEN_BROWSER:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
