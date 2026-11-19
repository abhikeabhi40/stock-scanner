"""
F&O SCANNER - Open Interest buildup for NSE stock futures
===========================================================
For every NSE stock that has F&O (futures & options) contracts, compares
today's near-month futures price and Open Interest (OI) against
yesterday's close to classify the move:

  LONG BUILDUP    price UP   + OI UP    -> fresh bullish bets being added
  SHORT BUILDUP   price DOWN + OI UP    -> fresh bearish bets being added
  SHORT COVERING  price UP   + OI DOWN  -> bears closing out (can fade fast)
  LONG UNWINDING  price DOWN + OI DOWN  -> bulls closing out (can fade fast)

This is a DIFFERENT signal than the cash scanners (volume/news) - it shows
what other F&O traders are actually positioning for, not a prediction.
LONG/SHORT BUILDUP are generally read as higher-conviction than
COVERING/UNWINDING, which can reverse fast.

Only ~180-200 NSE stocks have F&O contracts at all - most small/mid caps
don't, so this list will always be much shorter than the cash scanners.

Modes: "AUTO" (LIVE during market hours, else BACKTEST using NSE's own
end-of-day F&O file - no broker login needed for BACKTEST at all).
Read-only: places no orders. Login (LIVE only): angel_config.py (same folder).
"""

import io
import os
import sys
import time
import zipfile
from datetime import datetime, timedelta
from io import StringIO

sys.dont_write_bytecode = True

import warnings
warnings.filterwarnings("ignore")

import pandas as pd
import requests

# ============================================================
# SETTINGS
# ============================================================

MODE = "AUTO"
TEST_DATE = os.getenv("SCANNER_TEST_DATE") or ""
NSE_HOLIDAYS_2026 = ["2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
                     "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28",
                     "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20",
                     "2026-11-10", "2026-11-24", "2026-12-25"]
SCAN_TIME = os.getenv("SCANNER_SCAN_TIME") or "09:45"
TOP_N = 10

MIN_ABS_PRICE_CHANGE = 0.5   # % - ignore near-flat contracts
MIN_ABS_OI_CHANGE = 3.0      # % - ignore tiny day-to-day OI noise
MARGIN_PCT_EST = 0.15        # rough SPAN+exposure margin guess (varies by stock/broker)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


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


now = datetime.now()
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
print(f"MODE: {MODE} | DATE: {TEST_DATE} | SCAN TIME: {SCAN_TIME}")


def prev_trading_day(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5 or d.strftime("%Y-%m-%d") in NSE_HOLIDAYS_2026:
        d -= timedelta(days=1)
    return d


# ============================================================
# NSE F&O BHAVCOPY (end-of-day file - already has OI, no login needed)
# ============================================================

nse = requests.Session()
nse.headers.update({
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})

try:
    nse.get("https://www.nseindia.com/", timeout=15)
except Exception:
    pass


def fetch_fo_bhav(d):
    """Download one day's NSE F&O bhavcopy, stock-futures rows only. DataFrame or None."""
    if d.weekday() >= 5:
        return None
    url = ("https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    for attempt in range(3):
        try:
            r = nse.get(url, timeout=30)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                raw = z.read(z.namelist()[0]).decode("utf-8", "ignore")
            df = pd.read_csv(StringIO(raw))
            instr_col = pick_col(df.columns, "FinInstrmTp", "INSTRUMENT")
            if instr_col is None:
                print(f"  {d}: no instrument-type column; columns were: {list(df.columns)[:20]}")
                return None
            vals = df[instr_col].astype(str).str.upper().unique().tolist()
            stock_fut_tags = [v for v in vals if v in ("STF", "FUTSTK")]
            if not stock_fut_tags:
                print(f"  {d}: no stock-future rows matched; {instr_col} values were: {vals[:12]}")
                return None
            df = df[df[instr_col].astype(str).str.upper().isin(stock_fut_tags)].copy()
            m = {"sym": pick_col(df.columns, "TckrSymb", "SYMBOL"),
                 "expiry": pick_col(df.columns, "XpryDt", "EXPIRY_DT"),
                 "o": pick_col(df.columns, "OpnPric", "OPEN"),
                 "h": pick_col(df.columns, "HghPric", "HIGH"),
                 "l": pick_col(df.columns, "LwPric", "LOW"),
                 "c": pick_col(df.columns, "ClsPric", "CLOSE"),
                 "oi": pick_col(df.columns, "OpnIntrst", "OPEN_INT"),
                 "val": pick_col(df.columns, "TtlTrfVal", "VAL_INLAKH")}
            missing = [k for k, v in m.items() if v is None]
            if missing:
                print(f"  {d}: missing columns {missing}; columns were: {list(df.columns)}")
                return None
            out = df[list(m.values())].copy()
            out.columns = list(m.keys())
            if out["expiry"].dtype == object:
                out["expiry"] = pd.to_datetime(out["expiry"], errors="coerce", dayfirst=True)
            out["date"] = d
            return out
        except Exception as err:
            if attempt == 2:
                print(f"  {d}: download failed ({err})")
            time.sleep(1.5 * (attempt + 1))
    return None


def pick_near_month(df):
    """Keep each symbol's nearest-expiry row (the most actively traded contract)."""
    if "expiry" in df.columns and df["expiry"].notna().any():
        return df.sort_values(["sym", "expiry"]).drop_duplicates("sym", keep="first")
    return df.sort_values("val", ascending=False).drop_duplicates("sym", keep="first")


prev_day = prev_trading_day(test_day)
print(f"Downloading NSE F&O file for {prev_day} (yesterday's baseline)...")
yprev = fetch_fo_bhav(prev_day)
if yprev is None:
    print("Could not get yesterday's F&O data - cannot compute OI buildup.")
    sys.exit(1)
yprev_near = pick_near_month(yprev).set_index("sym")
print(f"Yesterday's F&O stock-future universe: {len(yprev_near)} stocks")

# lot sizes + token lookup - public file, no login needed
lot_size, nfo_tokens = {}, {}
try:
    inst = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/"
                        "files/OpenAPIScripMaster.json", timeout=60).json()
    for i in inst:
        if i.get("exch_seg") == "NFO" and i.get("instrumenttype") == "FUTSTK":
            nm = str(i.get("name", "")).upper()
            lot_size.setdefault(nm, fnum(i.get("lotsize"), 0))
            nfo_tokens.setdefault(nm, []).append((str(i.get("expiry", "")), str(i.get("token"))))
    del inst
except Exception as err:
    print("Could not load Angel instrument master (lot sizes unavailable):", err)


def choose_near_token(sym):
    opts = nfo_tokens.get(sym) or []
    def key(o):
        try:
            return datetime.strptime(o[0], "%d%b%Y")
        except Exception:
            return datetime.max
    opts = sorted(opts, key=key)
    return opts[0] if opts else None


# ============================================================
# TODAY'S DATA: LIVE (Angel quotes) or BACKTEST (today's own bhavcopy)
# ============================================================

rows = []
if MODE == "LIVE":
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

    import atexit
    import shutil
    import tempfile
    _TMP = tempfile.mkdtemp(prefix="fo_")
    _cwd = os.getcwd()
    os.chdir(_TMP)
    try:
        from SmartApi import SmartConnect
    finally:
        os.chdir(_cwd)
    atexit.register(shutil.rmtree, _TMP, True)
    import pyotp
    api = SmartConnect(api_key=cr["ANGEL_API_KEY"])
    lg = api.generateSession(cr["ANGEL_CLIENT_CODE"], cr["ANGEL_PIN"],
                             pyotp.TOTP(cr["ANGEL_TOTP_SECRET"]).now())
    if not lg.get("status"):
        print("Login failed:", lg)
        sys.exit(1)

    by_tok = {}
    for s in yprev_near.index:
        picked = choose_near_token(s)
        if picked:
            by_tok[picked[1]] = s
    toks = list(by_tok)
    print(f"Fetching live F&O quotes for {len(toks)} stock futures...")

    _last = [0.0]

    def throttle(gap=0.5):
        w = gap - (time.time() - _last[0])
        if w > 0:
            time.sleep(w)
        _last[0] = time.time()

    for i in range(0, len(toks), 50):
        for attempt in range(3):
            throttle()
            try:
                r = api.getMarketData("FULL", {"NFO": toks[i:i + 50]})
                for q in (r.get("data") or {}).get("fetched") or []:
                    s = by_tok.get(str(q.get("symbolToken")))
                    if not s:
                        continue
                    price = fnum(q.get("ltp"))
                    oi = fnum(q.get("opnInterest"))
                    base = yprev_near.loc[s]
                    if price <= 0 or base["c"] <= 0 or base["oi"] <= 0:
                        continue
                    rows.append(dict(symbol=s, price=price, prev_price=base["c"],
                                     oi=oi, prev_oi=base["oi"]))
                break
            except Exception as err:
                time.sleep(2)
                if attempt == 2:
                    print("  quote error:", err)
    if rows and all(r["oi"] <= 0 for r in rows):
        print("All contracts show zero OI - market may be closed (holiday?).")
        sys.exit(0)
else:
    print(f"Downloading NSE F&O file for {TEST_DATE}...")
    today_fo = fetch_fo_bhav(test_day)
    if today_fo is None:
        print(f"No NSE F&O data for {TEST_DATE} yet (file not published, or holiday).")
        sys.exit(1)
    today_near = pick_near_month(today_fo).set_index("sym")
    for s in today_near.index:
        if s not in yprev_near.index:
            continue
        cur, base = today_near.loc[s], yprev_near.loc[s]
        if cur["c"] <= 0 or base["c"] <= 0 or base["oi"] <= 0:
            continue
        rows.append(dict(symbol=s, price=cur["c"], prev_price=base["c"],
                         oi=cur["oi"], prev_oi=base["oi"]))

print(f"F&O stocks with usable data: {len(rows)}")

# ============================================================
# CLASSIFY + SCORE
# ============================================================

results = []
for r in rows:
    price_chg = (r["price"] - r["prev_price"]) / r["prev_price"] * 100
    oi_chg = (r["oi"] - r["prev_oi"]) / r["prev_oi"] * 100
    if abs(price_chg) < MIN_ABS_PRICE_CHANGE or abs(oi_chg) < MIN_ABS_OI_CHANGE:
        continue
    if price_chg > 0 and oi_chg > 0:
        buildup, conviction = "LONG BUILDUP", 2
    elif price_chg < 0 and oi_chg > 0:
        buildup, conviction = "SHORT BUILDUP", 2
    elif price_chg > 0 and oi_chg < 0:
        buildup, conviction = "SHORT COVERING", 1
    else:
        buildup, conviction = "LONG UNWINDING", 1
    lot = int(lot_size.get(r["symbol"], 0))
    margin_lakh = round(r["price"] * lot * MARGIN_PCT_EST / 1e5, 1) if lot else None
    score = conviction * 20 + abs(price_chg) * 3 + min(abs(oi_chg), 50)
    results.append(dict(symbol=r["symbol"], price=round(r["price"], 2),
                        change=round(price_chg, 2), oi_change=round(oi_chg, 2),
                        buildup=buildup, lot_size=lot, margin_est_lakh=margin_lakh,
                        score=round(score, 1)))

results.sort(key=lambda x: x["score"], reverse=True)
top = results[:TOP_N]

print("\n" + "=" * 112)
print(f"F&O OPEN INTEREST at {SCAN_TIME} on {TEST_DATE}  "
      f"(vs {prev_day}, {len(results)} stocks moved meaningfully)")
print("=" * 112)
if not top:
    print("  nothing crossed the minimum price-move / OI-change thresholds")
for i, m in enumerate(top, 1):
    lot_s = f"lot {m['lot_size']}" if m["lot_size"] else "lot ?"
    margin_s = f" ~{m['margin_est_lakh']}L margin*" if m["margin_est_lakh"] else ""
    print(f"{i:>2}. {m['symbol']:<13} {m['buildup']:<15} price {m['price']:>9.2f} "
          f"({m['change']:+5.1f}%) | OI {m['oi_change']:+6.1f}% | {lot_s}{margin_s}")
print("\n* margin is a rough estimate only (~15% of contract value) - check your broker for the real figure.")
print("LONG/SHORT BUILDUP = fresh positions, usually higher conviction.")
print("SHORT COVERING/LONG UNWINDING = positions closing, can reverse fast.")

if os.getenv("SCANNER_JSON_OUT"):
    import json
    with open(os.environ["SCANNER_JSON_OUT"], "w") as fh:
        json.dump({"mode": MODE, "date": TEST_DATE, "scan_time": SCAN_TIME,
                   "top": top, "all": results}, fh, default=str)
