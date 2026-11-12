"""
RESULTS-DAY SCANNER - stocks reacting strongly to quarterly results
===================================================================
Many 10-20% one-day moves happen on results day. This scanner:
  1. Gets from NSE every company whose results were declared yesterday
     (after market) or today, plus companies with a results board meeting
     today (results may come during market hours).
  2. Checks ONLY those stocks for a strong early reaction:
     price up, heavy volume, above VWAP, near the day's high, 20% band.
  3. Ranks them and prints entry / stop / targets.

LIVE (market hours): run at ~9:30. Set REPEAT_EVERY_MIN = 5 to re-check
  every 5 minutes until 3:00 PM - catches results announced during market.
BACKTEST (any other time): set TEST_DATE / SCAN_TIME; shows what each pick
  actually did for the rest of the day.

Nothing is saved to disk. Read-only: places no orders.
Login: angel_config.py (same folder).
"""

import atexit
import io
import os
import shutil
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import StringIO

sys.dont_write_bytecode = True          # no __pycache__ folder

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import pyotp
import requests

# SmartAPI library writes a "logs" folder where it is imported.
# Import it inside a temporary folder so nothing appears in your folder.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TMP = tempfile.mkdtemp(prefix="shocker_")
_cwd = os.getcwd()
os.chdir(_TMP)
try:
    from SmartApi import SmartConnect
finally:
    os.chdir(_cwd)
atexit.register(shutil.rmtree, _TMP, True)

# ============================================================
# SETTINGS
# ============================================================

MODE = "AUTO"
# BACKTEST date:  ""  = automatic (most recent trading day)
#                 "2026-11-04" = that specific day
TEST_DATE = os.getenv("SCANNER_TEST_DATE") or ""
NSE_HOLIDAYS_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
                     "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
                     "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
                     "2026-11-10", "2026-11-24", "2026-12-25"]
SCAN_TIME = os.getenv("SCANNER_SCAN_TIME") or "09:30"          # BACKTEST decision time (LIVE uses current time)
TOP_N = 10
REPEAT_EVERY_MIN = 0         # LIVE only: 5 = re-scan every 5 min until STOP_AT
STOP_AT = "15:00"

# Which results count
INCLUDE_DUE_TODAY = True     # board meeting today, results not filed yet
RESULT_FILED_BONUS = 10      # score bonus when results already published

# Hard rejects
MIN_PRICE = 20
MIN_CIRCUIT_BAND = 20        # 2/5/10% band stocks can't make a 10-20% day
MIN_VOL_PCT_HARD = 20        # % of avg full-day volume traded by SCAN_TIME
MIN_TURNOVER_HARD_CR = 0.3   # Rs crore traded by SCAN_TIME
MIN_AVG_TURNOVER_CR = 0.3    # 20-day avg daily turnover (skip illiquid)

# Grade A (miss some -> grade B, still listed)
MIN_VOL_PCT = 50
MIN_CHANGE = 1.0             # % vs previous close
MAX_CHANGE = 15.0
MIN_TURNOVER_CR = 2.0
MAX_DIST_FROM_HIGH = 1.5     # % below day's high so far
REQUIRE_ABOVE_VWAP = True

CHECK_NEWS = True
NEWS_LOOKBACK_DAYS = 3
NEWS_SCORE_BONUS = 10
REJECT_NEGATIVE_NEWS = True

# Targets and stop are set PER STOCK from its normal daily range (ADR =
# average high-low move of the last 20 days). Bigger movers get bigger targets.
TARGETS = [5.0, 10.0, 20.0]  # T1 / T2 / T3, % above entry
STOP_LOSS = 3.0              # % below entry
HOLD_DAYS = 3                # exit at the close of day 3 if no stop / target

SAVE_RESULTS = False         # True = save shockers CSV + shocker_log.csv
HISTORY_CAL_DAYS = 35        # enough for a 20-day average
THREADS = 4

POSITIVE_KEYWORDS = ["order", "contract", "award", "bagged", "secure", "wins",
                     "letter of intent", "financial result", "results", "acquisition",
                     "acquire", "merger", "bonus", "split", "buyback", "fund raising",
                     "preferential", "qip", "expansion", "capacity", "approval", "launch",
                     "partnership", "agreement", "upgrade", "dividend", "joint venture"]
NEGATIVE_KEYWORDS = ["resignation", "default", "penalty", "fraud", "raid", "pledge",
                     "downgrade", "fire", "shutdown", "suspension", "insolvency",
                     "show cause"]
IGNORE_KEYWORDS = ["trading window", "loss of share certificate", "duplicate share",
                   "compliance certificate", "newspaper publication", "investor meet",
                   "analyst", "regulation 74", "closure of trading"]

# ============================================================
# BASICS
# ============================================================

T_START = time.time()
now = datetime.now()

# ---- repeat mode: re-run this script every REPEAT_EVERY_MIN minutes ----
if (REPEAT_EVERY_MIN and os.getenv("RESULTS_SCANNER_CHILD") != "1"
        and MODE in ("AUTO", "LIVE")):
    import subprocess
    env = dict(os.environ, RESULTS_SCANNER_CHILD="1")
    stop_t = datetime.strptime(STOP_AT, "%H:%M").time()
    while datetime.now().time() < stop_t:
        subprocess.run([sys.executable, os.path.abspath(__file__)], env=env)
        nxt = datetime.now() + timedelta(minutes=REPEAT_EVERY_MIN)
        print(f"\nNext scan at {nxt:%H:%M}  (Ctrl+C to stop)\n")
        while datetime.now() < nxt:
            time.sleep(5)
    sys.exit(0)


def hm(t):
    return datetime.strptime(t, "%H:%M").time()


def fnum(v, d=0.0):
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return d


def pick_col(cols, *names):
    up = {c.strip().upper(): c for c in cols}
    for n in names:
        if n.upper() in up:
            return up[n.upper()]
    return None


if MODE == "AUTO":
    MODE = "LIVE" if (now.weekday() < 5 and hm("09:15") <= now.time() <= hm("15:30")) \
        else "BACKTEST"
if MODE == "BACKTEST" and not TEST_DATE:
    _d = now.date() - (timedelta(days=0) if now.time() >= hm("15:30") else timedelta(days=1))
    while _d.weekday() >= 5 or _d.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        _d -= timedelta(days=1)
    TEST_DATE = _d.strftime("%Y-%m-%d")
    print(f"Automatic date: {TEST_DATE} (most recent trading day)")
if MODE == "LIVE":
    TEST_DATE = now.strftime("%Y-%m-%d")
    SCAN_TIME = now.strftime("%H:%M")
test_day = datetime.strptime(TEST_DATE, "%Y-%m-%d").date()
scan_t = hm(SCAN_TIME)
TODAY = now.date()
print(f"MODE: {MODE} | DATE: {TEST_DATE} | SCAN TIME: {SCAN_TIME}")

# ============================================================
# NSE BHAVCOPY (shared cache with swing scanner)
# ============================================================

nse = requests.Session()
nse.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})


def nse_warmup():
    try:
        nse.get("https://www.nseindia.com/", timeout=15)
    except Exception:
        pass


nse_warmup()


def fetch_bhav(d):
    """Download one day's NSE file into memory. Returns DataFrame or None."""
    if d.weekday() >= 5:
        return None
    url = ("https://nsearchives.nseindia.com/content/cm/"
           f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    for attempt in range(3):
        try:
            r = nse.get(url, timeout=30)
            if r.status_code == 404:
                return None                      # holiday / not published
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
            df = pd.read_csv(StringIO(raw))
            m = {"sym": pick_col(df.columns, "TckrSymb", "SYMBOL"),
                 "series": pick_col(df.columns, "SctySrs", "SERIES"),
                 "o": pick_col(df.columns, "OpnPric", "OPEN"),
                 "h": pick_col(df.columns, "HghPric", "HIGH"),
                 "l": pick_col(df.columns, "LwPric", "LOW"),
                 "c": pick_col(df.columns, "ClsPric", "CLOSE"),
                 "prev": pick_col(df.columns, "PrvsClsgPric", "PREVCLOSE"),
                 "v": pick_col(df.columns, "TtlTradgVol", "TOTTRDQTY"),
                 "val": pick_col(df.columns, "TtlTrfVal", "TOTTRDVAL")}
            if any(v is None for v in m.values()):
                print(f"  {d}: unexpected bhavcopy columns")
                return None
            out = df[list(m.values())].copy()
            out.columns = list(m.keys())
            out["series"] = out["series"].astype(str).str.strip()
            out = out[out["series"].isin(("EQ", "BE"))].copy()
            out["date"] = d
            time.sleep(0.2)
            return out
        except Exception as err:
            if attempt == 2:
                print(f"  {d}: download failed ({err})")
            time.sleep(1.5 * (attempt + 1))
    return None


# History needed: 20 trading days BEFORE test day (+ test day itself for backtest)
days = [test_day - timedelta(days=k) for k in range(HISTORY_CAL_DAYS, -8, -1)]
todo = [d for d in days if d.weekday() < 5 and d <= TODAY]
print(f"Downloading {len(todo)} NSE daily files (kept in memory only)...")
with ThreadPoolExecutor(THREADS) as ex:
    frames = [f for f in ex.map(fetch_bhav, todo) if f is not None]
if not frames:
    print("No NSE daily files available - check internet and re-run.")
    sys.exit(1)
panel = (pd.concat(frames, ignore_index=True)
         .sort_values(["sym", "date", "series"])
         .drop_duplicates(["sym", "date"], keep="first"))

hist = panel[panel["date"] < test_day]
hist_days = sorted(hist["date"].unique())[-20:]
if len(hist_days) < 10:
    print("Not enough history before", TEST_DATE)
    sys.exit(1)
hist = hist[hist["date"].isin(hist_days)].sort_values(["sym", "date"])


# split/bonus-safe 20-day stats: adjust volume using NSE previous close
def stock_stats(g):
    f = (g["prev"] / g["c"].shift(1)).fillna(1.0)
    f[(f - 1).abs() < 0.005] = 1.0
    cum = f[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    last = g.iloc[-1]
    return pd.Series({
        "avg_vol": (g["v"] / cum).mean(),
        "avg_turn_cr": g["val"].mean() / 1e7,
        "high_20d": (g["h"] * cum).max(),
        "prev_close": last["c"],
        "days": len(g),
        "adr": ((g["h"] - g["l"]) / g["prev"].where(g["prev"] > 0)).mean() * 100,
    })


def levels(adr=None):
    """Fixed stop % and target % list (same for every stock)."""
    return STOP_LOSS, list(TARGETS)


stats = hist.groupby("sym").apply(stock_stats)
stats = stats[(stats["days"] >= 10) & (stats["avg_turn_cr"] >= MIN_AVG_TURNOVER_CR)
              & (stats["prev_close"] >= MIN_PRICE * 0.8)]
print(f"History ready ({len(hist_days)} days): {len(stats)} liquid stocks")

# circuit bands
bands = {}
try:
    r = nse.get("https://nsearchives.nseindia.com/content/equities/sec_list.csv", timeout=30)
    b = pd.read_csv(StringIO(r.text))
    b.columns = [c.strip().upper() for c in b.columns]
    sc = next(c for c in b.columns if "SYMBOL" in c)
    bc = next(c for c in b.columns if "BAND" in c)
    for _, row in b.iterrows():
        raw = str(row[bc]).upper()
        bands[str(row[sc]).strip().upper()] = "NO BAND" if "NO" in raw else fnum(raw, 0)
    keep = [s for s in stats.index
            if bands.get(s) == "NO BAND" or (bands.get(s) or 0) >= MIN_CIRCUIT_BAND]
    stats = stats.loc[keep]
    print(f"After circuit-band filter (>= {MIN_CIRCUIT_BAND}% / no band): {len(stats)}")
except Exception as err:
    print("Band list unavailable (not filtering by band):", err)

# ============================================================
# ANGEL LOGIN + TOKENS
# ============================================================

KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET")
cfg = {}
try:
    sys.path.insert(0, _SCRIPT_DIR)
    import angel_config as ac
    cfg = {k: str(getattr(ac, k, "")).strip() for k in KEYS}
except ImportError:
    pass
cr = {k: cfg.get(k) or os.getenv(k) or "" for k in KEYS}
if not all(cr.values()):
    print("Missing Angel One details - fill angel_config.py")
    sys.exit(1)
api = SmartConnect(api_key=cr["ANGEL_API_KEY"])
lg = api.generateSession(cr["ANGEL_CLIENT_CODE"], cr["ANGEL_PIN"],
                         pyotp.TOTP(cr["ANGEL_TOTP_SECRET"]).now())
if not lg.get("status"):
    print("Login failed:", lg)
    sys.exit(1)

print("Downloading Angel stock list...")
inst = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/"
                    "files/OpenAPIScripMaster.json", timeout=60).json()
tokens = {str(i["symbol"])[:-3].upper(): str(i["token"]) for i in inst
          if i.get("exch_seg") == "NSE" and str(i.get("symbol", "")).endswith("-EQ")}
del inst
stats = stats[stats.index.isin(tokens.keys())]

# ============================================================
# RESULTS COMPANIES (from NSE)
# ============================================================


def parse_dt(text):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%d-%m-%Y %H:%M:%S",
                "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(text).strip(), fmt)
        except Exception:
            pass
    return None


def nse_json(url):
    for _ in range(3):
        try:
            r = nse.get(url, timeout=30)
            if r.status_code in (401, 403):
                nse_warmup()
                continue
            data = r.json()
            return data if isinstance(data, list) else data.get("data", [])
        except Exception:
            time.sleep(1.5)
    return None


prev_td = hist_days[-1]                       # previous trading day
scan_dt = datetime.combine(test_day, scan_t)
window_start = datetime.combine(prev_td, hm("09:00"))
res_info = {}

# 1) results already published (announcements)
ann = nse_json("https://www.nseindia.com/api/corporate-announcements?index=equities"
               f"&from_date={prev_td:%d-%m-%Y}&to_date={test_day:%d-%m-%Y}")
for it in ann or []:
    sym = str(it.get("symbol", "")).strip().upper()
    text = (str(it.get("desc", "")) + " " + str(it.get("attchmntText", ""))).lower()
    if "financial result" not in text and not ("outcome of board meeting" in text
                                                and "result" in text):
        continue
    t = parse_dt(it.get("an_dt") or it.get("sort_date"))
    if t is None or not (window_start <= t <= scan_dt):   # no look-ahead in backtest
        continue
    old = res_info.get(sym)
    if not old or old["status"] != "FILED" or t > old["time"]:
        res_info[sym] = {"status": "FILED", "time": t}

# 2) results board meeting scheduled today (results may come during market)
if INCLUDE_DUE_TODAY:
    bm = nse_json("https://www.nseindia.com/api/corporate-board-meetings?index=equities"
                  f"&from_date={test_day:%d-%m-%Y}&to_date={test_day:%d-%m-%Y}")
    for it in bm or []:
        sym = str(it.get("bm_symbol") or it.get("symbol") or "").strip().upper()
        text = (str(it.get("bm_purpose", "")) + " " + str(it.get("bm_desc", ""))).lower()
        if sym and "result" in text and sym not in res_info:
            res_info[sym] = {"status": "DUE TODAY", "time": None}

if ann is None and not res_info:
    print("Could not get results announcements from NSE (site busy?). Try again in a minute.")
    sys.exit(1)

n_filed = sum(1 for v in res_info.values() if v["status"] == "FILED")
n_due = len(res_info) - n_filed
print(f"Results companies: {n_filed} published (since {prev_td} 9:00 AM), "
      f"{n_due} with board meeting today")
stats = stats[stats.index.isin(res_info.keys())]
print(f"Liquid results stocks to check (band >= {MIN_CIRCUIT_BAND}%): {len(stats)}")
if stats.empty:
    print("No liquid results stocks to scan for this date.")
    sys.exit(0)

_last = [0.0]


def throttle(gap=0.36):
    w = gap - (time.time() - _last[0])
    if w > 0:
        time.sleep(w)
    _last[0] = time.time()

# ============================================================
# BUILD SNAPSHOT AT SCAN TIME
# ============================================================


def make_row(sym, price, open_px, day_high, vol, vwap, prev_close, extra=None):
    s = stats.loc[sym]
    prev_close = prev_close or s["prev_close"]
    if price <= 0 or prev_close <= 0:
        return None
    row = dict(symbol=sym, price=price, open=open_px, day_high=day_high, volume=vol,
               vwap=vwap, prev_close=prev_close, avg_vol=s["avg_vol"],
               high_20d=s["high_20d"], adr=s["adr"], band=bands.get(sym, "?"))
    if extra:
        row.update(extra)
    return row


snap = []
if MODE == "LIVE":
    syms = list(stats.index)
    print(f"Fetching live quotes for {len(syms)} stocks...")
    by_tok = {tokens[s]: s for s in syms}
    toks = list(by_tok)
    for i in range(0, len(toks), 50):
        for attempt in range(3):
            throttle(0.5)
            try:
                r = api.getMarketData("FULL", {"NSE": toks[i:i + 50]})
                for q in (r.get("data") or {}).get("fetched") or []:
                    s = by_tok.get(str(q.get("symbolToken")))
                    if s:
                        row = make_row(s, fnum(q.get("ltp")), fnum(q.get("open")),
                                       fnum(q.get("high")), fnum(q.get("tradeVolume")),
                                       fnum(q.get("avgPrice")) or None, fnum(q.get("close")))
                        if row:
                            snap.append(row)
                break
            except Exception as err:
                time.sleep(2)
                if attempt == 2:
                    print("  quote error:", err)
    if snap and all(r["volume"] <= 0 for r in snap):
        print("All stocks show zero volume - the market seems closed today (holiday?).")
        sys.exit(0)
else:
    day = panel[panel["date"] == test_day].set_index("sym")
    if day.empty:
        print(f"No NSE data for {TEST_DATE} (holiday, or file not published yet).")
        sys.exit(1)
    # Only stocks that COULD have qualified at SCAN_TIME:
    # full-day volume >= hard minimum and day high >= minimum change
    cand = []
    for s in stats.index:
        if s not in day.index:
            continue
        d = day.loc[s]
        st = stats.loc[s]
        if (d["h"] > d["prev"] * 1.0
                and d["val"] / 1e7 >= MIN_TURNOVER_HARD_CR):
            cand.append(s)
    print(f"Candidates that could qualify at {SCAN_TIME}: {len(cand)} "
          f"(of {len(stats)}) - fetching intraday data...")
    def intraday(sym):
        for attempt in range(4):
            throttle()
            try:
                r = api.getCandleData({"exchange": "NSE", "symboltoken": tokens[sym],
                                       "interval": "FIVE_MINUTE",
                                       "fromdate": f"{TEST_DATE} 09:15",
                                       "todate": f"{TEST_DATE} 15:30"})
                if r and r.get("status"):
                    return r.get("data") or []
            except Exception:
                pass
            time.sleep(2 + 2 * attempt)
        return []

    for n, s in enumerate(cand, 1):
        print(f"  {n}/{len(cand)} {s:<15}", end="\r")
        bars = []
        for b in intraday(s):
            try:
                t = datetime.fromisoformat(str(b[0])).replace(tzinfo=None)
                bars.append((t.time(), fnum(b[1]), fnum(b[2]), fnum(b[3]), fnum(b[4]),
                             fnum(b[5])))
            except Exception:
                pass
        bars.sort()
        early = [b for b in bars if b[0] < scan_t]
        after = [b for b in bars if b[0] >= scan_t]
        if not early:
            continue
        vol = sum(b[5] for b in early)
        pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in early)
        extra = {}
        price = early[-1][4]
        if after and price > 0:
            highs = [b[2] for b in after]
            lows = [b[3] for b in after]
            sl_pct, tgs = levels()
            # path = rest of today (5-min bars) + next HOLD_DAYS-1 days (daily bars)
            path = [(h, l, c) for _, _, h, l, c, _ in after]
            nxt = panel[(panel["sym"] == s) & (panel["date"] > test_day)].sort_values("date")
            nxt = nxt.head(HOLD_DAYS - 1)
            path += [(r_["h"], r_["l"], r_["c"]) for _, r_ in nxt.iterrows()]
            extra["days_data"] = 1 + len(nxt)
            highs = [x[0] for x in path]
            lows = [x[1] for x in path]
            extra["max_gain"] = (max(highs) - price) / price * 100
            extra["close_gain"] = (after[-1][4] - price) / price * 100
            extra["exit_gain"] = (path[-1][2] - price) / price * 100
            sl_i = next((i for i, l in enumerate(lows) if l <= price * (1 - sl_pct / 100)),
                        None)
            for k, t in enumerate(tgs, 1):
                hi_i = next((i for i, h in enumerate(highs) if h >= price * (1 + t / 100)), None)
                extra[f"hit_{k}"] = hi_i is not None and (sl_i is None or hi_i < sl_i)
            extra["sl_hit_first"] = sl_i is not None and not extra["hit_1"]
        extra["day_close_chg"] = (day.loc[s, "c"] / day.loc[s, "prev"] - 1) * 100
        row = make_row(s, price, early[0][1], max(b[2] for b in early), vol,
                       pv / vol if vol else None, day.loc[s, "prev"], extra)
        if row:
            snap.append(row)
    print()

# ============================================================
# FILTER + SCORE
# ============================================================

results = []
for r in snap:
    price, prev = r["price"], r["prev_close"]
    change = (price - prev) / prev * 100
    gap = (r["open"] - prev) / prev * 100 if r["open"] else 0
    vol_pct = r["volume"] / r["avg_vol"] * 100 if r["avg_vol"] else 0
    turnover = r["volume"] * (r["vwap"] or price) / 1e7
    dist_high = (r["day_high"] - price) / price * 100 if r["day_high"] else 0
    above_vwap = r["vwap"] is not None and price > r["vwap"]
    breakout = price > r["high_20d"]

    if price < MIN_PRICE or change <= 0 or turnover < MIN_TURNOVER_HARD_CR:
        continue
    fails = []
    if vol_pct < MIN_VOL_PCT: fails.append(f"vol {vol_pct:.0f}% of day")
    if not (MIN_CHANGE <= change <= MAX_CHANGE): fails.append(f"change {change:+.1f}%")
    if turnover < MIN_TURNOVER_CR: fails.append(f"turnover {turnover:.1f}cr")
    if dist_high > MAX_DIST_FROM_HIGH: fails.append(f"{dist_high:.1f}% off high")
    if REQUIRE_ABOVE_VWAP and not above_vwap: fails.append("below VWAP")

    score = min(vol_pct / 300, 1) * 35                       # 300% of a day = full
    score += 20 if 2 <= change <= 8 else (12 if MIN_CHANGE <= change <= MAX_CHANGE else 0)
    score += max(0, 1 - dist_high / 3) * 15
    score += 10 if above_vwap else 0
    score += 10 if breakout else 0
    score += min(turnover / 10, 1) * 10
    score -= 4 * len(fails)
    ri = res_info.get(r["symbol"], {})
    if ri.get("status") == "FILED":
        score += RESULT_FILED_BONUS
    r["results"] = (f"FILED {ri['time']:%d-%b %H:%M}" if ri.get("status") == "FILED"
                    else ri.get("status", "-"))
    r.update(change=change, gap=gap, vol_pct=vol_pct, turnover_cr=turnover,
             dist_high=dist_high, above_vwap=above_vwap, breakout_20d=breakout,
             fails=", ".join(fails), grade="A" if not fails else "B", score=score,
             news="-", news_items=[])
    results.append(r)

if not results:
    if os.getenv("SCANNER_JSON_OUT"):
        import json
        with open(os.environ["SCANNER_JSON_OUT"], "w") as fh:
            json.dump({"mode": MODE, "date": TEST_DATE, "scan_time": SCAN_TIME,
                       "top": [], "all": []}, fh)
    print("No results stock is reacting up with volume yet. Try a later SCAN_TIME, "
          "or use REPEAT_EVERY_MIN = 5 in LIVE mode.")
    sys.exit(0)

# ============================================================
# NEWS
# ============================================================


def classify(text):
    t = text.lower()
    if any(k in t for k in IGNORE_KEYWORDS):
        return "IGNORE"
    if any(k in t for k in NEGATIVE_KEYWORDS):
        return "NEGATIVE"
    if any(k in t for k in POSITIVE_KEYWORDS):
        return "POSITIVE"
    return "NEUTRAL"


def get_news(sym):
    frm = test_day - timedelta(days=NEWS_LOOKBACK_DAYS)
    url = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           f"&symbol={sym}&from_date={frm:%d-%m-%Y}&to_date={test_day:%d-%m-%Y}")
    for _ in range(2):
        try:
            r = nse.get(url, timeout=20)
            if r.status_code in (401, 403):
                nse_warmup()
                continue
            data = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            out = []
            for it in items:
                subj = str(it.get("desc") or it.get("subject") or "")
                det = str(it.get("attchmntText") or "")
                tag = classify(subj + " " + det)
                if tag != "IGNORE":
                    out.append({"subject": subj, "tag": tag})
            return out
        except Exception:
            time.sleep(1)
    return None


def verdict(news):
    if news is None:
        return "UNKNOWN"
    tags = {n["tag"] for n in news}
    return ("NEGATIVE" if "NEGATIVE" in tags else "POSITIVE" if "POSITIVE" in tags
            else "NEUTRAL" if news else "NONE")


results.sort(key=lambda x: (x["grade"] == "A", x["score"]), reverse=True)
short = results[:TOP_N * 2]
if CHECK_NEWS:
    print(f"Checking NSE announcements for {len(short)} stocks...")
    with ThreadPoolExecutor(3) as ex:
        news_list = list(ex.map(get_news, [m["symbol"] for m in short]))
    for m, news in zip(short, news_list):
        m["news"] = verdict(news)
        m["news_items"] = news or []
        if m["news"] == "POSITIVE":
            m["score"] += NEWS_SCORE_BONUS
    if REJECT_NEGATIVE_NEWS:
        short = [m for m in short if m["news"] != "NEGATIVE"]
short.sort(key=lambda x: (x["grade"] == "A", x["score"]), reverse=True)
top = short[:TOP_N]

# ============================================================
# OUTPUT
# ============================================================

n_a = sum(1 for m in results if m["grade"] == "A")
print("\n" + "=" * 118)
print(f"TOP {TOP_N} RESULTS-DAY STOCKS at {SCAN_TIME} on {TEST_DATE}  "
      f"({len(results)} reacting up with volume, {n_a} grade A)")
print("=" * 118)
for i, m in enumerate(top, 1):
    print(f"{i:>2}. [{m['grade']}] {m['symbol']:<13} score {m['score']:>5.0f} | "
          f"results {m['results']:<17} | price {m['price']:>9.2f} | chg {m['change']:+5.1f}% | "
          f"gap {m['gap']:+5.1f}% | VOL {m['vol_pct']:4.0f}% of day | {m['turnover_cr']:5.1f}cr | "
          f"VWAP {'Y' if m['above_vwap'] else 'N'} | band {m['band']}")
    if m["fails"]:
        print(f"         missed: {m['fails']}")

if CHECK_NEWS:
    print("\nNEWS")
    for m in top:
        heads = [n for n in m["news_items"] if n["tag"] != "NEUTRAL"][:3] or m["news_items"][:2]
        txt = " | ".join(f"[{n['tag']}] {n['subject']}" for n in heads) or "no announcements"
        print(f"  {m['symbol']:<13} {txt[:150]}")

print("\nTRADE PLAN (if you take it)")
for m in top:
    e = m["price"]
    sl, tg = levels()
    print(f"  {m['symbol']:<13} entry ~{e:.2f} | stop {e * (1 - sl / 100):.2f} (-{sl:g}%) | "
          + " | ".join(f"T{k} {e * (1 + t / 100):.2f} (+{t:g}%)" for k, t in enumerate(tg, 1))
          + f" | hold max {HOLD_DAYS} days")

if MODE == "BACKTEST" and top and "max_gain" in top[0]:
    print("\n" + "=" * 118)
    print(f"BACKTEST: entry at {SCAN_TIME} price, held up to {HOLD_DAYS} days "
          f"(stop -{STOP_LOSS:g}%, targets +{TARGETS[0]:g}/+{TARGETS[1]:g}/+{TARGETS[2]:g}%)")
    print("=" * 118)
    for m in top:
        if "max_gain" not in m:
            continue
        _, tg = levels()
        hits = " ".join(f"T{k}(+{t:g}%):{'YES' if m.get(f'hit_{k}') else 'no '}"
                        for k, t in enumerate(tg, 1))
        print(f"  [{m['grade']}] {m['symbol']:<13} max {m['max_gain']:+6.2f}% in {m['days_data']}d | "
              f"day1 close {m['close_gain']:+6.2f}% | day{m['days_data']} close {m['exit_gain']:+6.2f}% | "
              f"{hits} | SL first: {'YES' if m['sl_hit_first'] else 'no'}")
    done = [m for m in top if "max_gain" in m]
    for k in (1, 2, 3):
        print(f"  Hit T{k} before stop: {sum(1 for m in done if m.get(f'hit_{k}'))}/{len(done)}")
    print(f"  Average if held to day-{HOLD_DAYS} close: "
          f"{sum(m['exit_gain'] for m in done) / len(done):+.2f}%")
    if done and done[0]["days_data"] < HOLD_DAYS:
        print(f"  (only {done[0]['days_data']} day(s) of data after {TEST_DATE} so far)")

    # Real top gainers of the day (whole market, from NSE file) and our rank
    day = panel[panel["date"] == test_day].copy()
    day = day[day["sym"].isin(stats.index)]
    day["chg"] = (day["c"] / day["prev"] - 1) * 100
    rank = {m["symbol"]: i for i, m in enumerate(results, 1)}
    print(f"\n  Day's real top 10 gainers AMONG RESULTS STOCKS (band >= {MIN_CIRCUIT_BAND}%) "
          f"and where they were at {SCAN_TIME}:")
    for _, g in day.sort_values("chg", ascending=False).head(10).iterrows():
        where = (f"rank #{rank[g['sym']]} in our list" if g["sym"] in rank
                 else "not flagged at scan time")
        print(f"    {g['sym']:<13} day {g['chg']:+6.2f}% | {where}")

if SAVE_RESULTS:
    stamp = f"{TEST_DATE}_{SCAN_TIME.replace(':', '')}"
    df = pd.DataFrame(results)
    df["news_headlines"] = df["news_items"].apply(
        lambda it: " | ".join(f"[{n['tag']}] {n['subject']}" for n in it) if it else "")
    out = os.path.join(_SCRIPT_DIR, f"shockers_{stamp}.csv")
    df.drop(columns=["news_items"]).to_csv(out, index=False)
    log = os.path.join(_SCRIPT_DIR, "shocker_log.csv")
    pd.DataFrame([dict(m, run_mode=MODE, date=TEST_DATE, scan_time=SCAN_TIME, rank=i)
                  for i, m in enumerate(top, 1)]).drop(columns=["news_items"]).to_csv(
        log, mode="a", header=not os.path.exists(log), index=False)
    print(f"\nSaved shockers_{stamp}.csv, appended top {TOP_N} to shocker_log.csv")
else:
    print("\nNothing saved to disk (set SAVE_RESULTS = True to save CSV files).")
print(f"Total time: {time.time() - T_START:.0f} sec")

# hand results to morning_scanner.py (when it asks for them)
if os.getenv("SCANNER_JSON_OUT"):
    import json
    with open(os.environ["SCANNER_JSON_OUT"], "w") as fh:
        json.dump({"mode": MODE, "date": TEST_DATE, "scan_time": SCAN_TIME,
                   "top": top, "all": results}, fh, default=str)
