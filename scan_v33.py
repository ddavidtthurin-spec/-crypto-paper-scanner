#!/usr/bin/env python3
"""v3.3.3 live scan — same A-gate, tighter confirm + alt flow.
Universe = OKX USDT top80 vol each run.
4H on all 80 (100 bars). 1H on ranked structure candidates (max 25).
15m confirm on A/near-A only — never creates A.
Alt funding/OI on A/B + unconf/hl (max 10). Improving never buys.
Hold = completed candle only. Learning never buys.
"""
import atexit, json, subprocess, time, datetime, statistics, os, shutil, tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DATA_DIR = Path(os.environ.get("SCANNER_DATA_DIR", Path(__file__).resolve().parent))
STATE = Path(os.environ.get("SCANNER_STATE", DATA_DIR / "crypto_daytrade_state.json"))
OUT = Path(os.environ.get("SCANNER_OUT", DATA_DIR / "scan_live.json"))
LOCK = Path(os.environ.get("SCANNER_LOCK", DATA_DIR / ".scan_v33.lock"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Prevent two hourly jobs from reading and overwriting the same state concurrently.
_lock_fh = open(LOCK, "a+")
try:
    import fcntl
    fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit("another scanner process is already running")
atexit.register(_lock_fh.close)

def load_state(path):
    """Fail safely; use the last atomic backup if the primary JSON is corrupt."""
    if not path.exists():
        return {}
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        try:
            with candidate.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    raise SystemExit(f"state is unreadable: {path} (and backup)")

def atomic_json_write(path, payload, ensure_ascii=True):
    """Write complete JSON or leave the previous good file untouched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=ensure_ascii)
            fh.flush()
            os.fsync(fh.fileno())
        if path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

STABLES = {
    "USDT","USDC","USDE","DAI","TUSD","FDUSD","USD0","USDD","PYUSD","EUR","USD",
    "USDG","USDJ","EURI","AEUR","CUSD","BUSD","GUSD","FRAX","LUSD","CRVUSD",
    "USDP","USTC","USDS","USD1"
}
CRYPTO_X = {"XRP","XLM","XMR","XTZ","XEC","XYO","XCH","XCN","XDC","XEM","XNO"}

def now_cest():
    return datetime.datetime.now(ZoneInfo("Europe/Stockholm"))

def curl_json(url, retries=5, sleep=0.35):
    last = None
    for i in range(retries):
        try:
            p = subprocess.run(
                ["curl","-sS","-A",UA,"--max-time","20",url],
                capture_output=True, text=True, timeout=25
            )
            if p.returncode != 0:
                last = f"curl_rc={p.returncode} {(p.stderr or '')[:160]}"
                time.sleep(sleep*(i+1)); continue
            txt = (p.stdout or "").strip()
            if not txt.startswith("{") and not txt.startswith("["):
                last = txt[:160]
                time.sleep(sleep*(i+1)); continue
            d = json.loads(txt)
            if isinstance(d, dict) and str(d.get("code")) == "50011":
                last = "50011"
                time.sleep(0.55*(i+1)); continue
            return d
        except Exception as e:
            last = str(e)
            time.sleep(sleep*(i+1))
    return {"_error": last}

def is_skip_base(base):
    if base in STABLES:
        return True
    if base in {"XAUT","PAXG","PAX"}:
        return True
    if base.startswith("X") and base not in CRYPTO_X and len(base) >= 3:
        return True
    return False

def fnum(x):
    try:
        return float(x)
    except Exception:
        return None

def pr_in_range(last, high, low):
    last, high, low = fnum(last), fnum(high), fnum(low)
    if last is None or high is None or low is None or high <= low:
        return None
    return 100.0 * (last - low) / (high - low)

def chg_pct(last, open24):
    last, open24 = fnum(last), fnum(open24)
    if last is None or open24 in (None, 0):
        return None
    return 100.0 * (last / open24 - 1)

def parse_candles(payload):
    if not isinstance(payload, dict) or payload.get("code") != "0":
        return None
    out = []
    for c in payload.get("data") or []:
        out.append({
            "ts": int(c[0]),
            "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]),
            "v": float(c[5]),
            "confirm": str(c[8]) if len(c) > 8 else "0",
        })
    out.sort(key=lambda x: x["ts"])
    return out

def compact_bar(c):
    return [c["ts"], c["o"], c["h"], c["l"], c["c"], c["v"], str(c.get("confirm", "1"))]

def expand_bar(row):
    return {
        "ts": int(row[0]), "o": float(row[1]), "h": float(row[2]),
        "l": float(row[3]), "c": float(row[4]), "v": float(row[5]),
        "confirm": str(row[6]) if len(row) > 6 else "1",
    }

def merge_candles(cached, live, max_n=120):
    by_ts = {}
    for c in cached or []:
        if isinstance(c, list) and len(c) >= 6:
            by_ts[int(c[0])] = expand_bar(c)
        elif isinstance(c, dict) and "ts" in c:
            by_ts[int(c["ts"])] = c
    for c in live or []:
        by_ts[int(c["ts"])] = c
    out = [by_ts[k] for k in sorted(by_ts)]
    return out[-max_n:]

def split_completed(cs):
    if not cs:
        return [], None
    forming = cs[-1] if str(cs[-1].get("confirm")) != "1" else None
    completed = [c for c in cs if str(c.get("confirm")) == "1"]
    if forming is None and cs:
        completed = cs[:]
    return completed, forming

def atr_n(completed, n=14):
    if not completed or len(completed) < 3:
        return None
    trs = []
    for i in range(1, len(completed)):
        h, l, pc = completed[i]["h"], completed[i]["l"], completed[i-1]["c"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    use = trs[-n:]
    if not use:
        return None
    return sum(use) / len(use)

def candle_shape(bar):
    h, l, o, c = bar["h"], bar["l"], bar["o"], bar["c"]
    rng = h - l
    if rng <= 0:
        return {"rng": 0, "body": 0, "up_wick": 0, "dn_wick": 0, "close_loc": 50.0, "bull": c >= o}
    body = abs(c - o)
    up = h - max(o, c)
    dn = min(o, c) - l
    return {
        "rng": rng,
        "body": body / rng,
        "up_wick": up / rng,
        "dn_wick": dn / rng,
        "close_loc": 100.0 * (c - l) / rng,
        "bull": c >= o,
    }

def swing_pivots(completed, left=2, right=2):
    sh, sl = [], []
    n = len(completed)
    if n < left + right + 1:
        return sh, sl
    end = n - right
    for i in range(left, end):
        h = completed[i]["h"]
        l = completed[i]["l"]
        if all(h >= completed[i-j]["h"] for j in range(1, left+1)) and all(h > completed[i+j]["h"] for j in range(1, right+1)):
            sh.append(i)
        if all(l <= completed[i-j]["l"] for j in range(1, left+1)) and all(l < completed[i+j]["l"] for j in range(1, right+1)):
            sl.append(i)
    return sh, sl

def empty_st(n=0, forming=None):
    return {
        "ok": False, "n": n,
        "bh": False, "fb": False, "hl": False, "pullback": False,
        "rh": None, "rl": None, "pr_struct": None,
        "break_pct": None, "last_close": None,
        "forming_close": None if not forming else forming["c"],
        "forming": forming is not None,
        "break_unconfirmed": False,
        "atr": None, "vol_ratio": None,
        "up_wick": None, "dn_wick": None, "close_loc": None,
        "hh": False, "hl_swings": False, "n_sh": 0, "n_sl": 0,
        "weak_hold": False, "thin_vol": False,
    }

def classify_tf(cs, min_n=12):
    """Completed-only structure. Forming never creates HOLD."""
    if not cs or len(cs) < 6:
        return empty_st(0 if not cs else len(cs))
    completed, forming = split_completed(cs)
    if len(completed) < min_n:
        st = empty_st(len(cs), forming)
        st["n"] = len(cs)
        return st
    last = completed[-1]
    look = completed[:-1]
    win = look[-40:] if len(look) >= 12 else look
    sh, sl = swing_pivots(completed, 2, 2)
    sh_ok = [i for i in sh if i < len(completed) - 1]
    sl_ok = [i for i in sl if i < len(completed) - 1]
    if sh_ok:
        rh = completed[sh_ok[-1]]["h"]
        roll = max(c["h"] for c in win)
        if roll > rh * 1.002 and sh_ok[-1] < len(completed) - 6:
            rh = roll
    else:
        rh = max(c["h"] for c in win)
    if sl_ok:
        rl = completed[sl_ok[-1]]["l"]
        roll_l = min(c["l"] for c in win)
        if roll_l < rl * 0.998 and sl_ok[-1] < len(completed) - 6:
            rl = roll_l
    else:
        rl = min(c["l"] for c in win)
    rng = rh - rl
    close = last["c"]
    pr_struct = None if rng <= 0 else 100.0 * (close - rl) / rng
    break_pct = None if rh <= 0 else 100.0 * (close / rh - 1)
    shp = candle_shape(last)
    atr = atr_n(completed, 14)
    vols = [c["v"] for c in completed[-21:-1] if c.get("v")]
    vol_sma = (sum(vols) / len(vols)) if vols else None
    vol_ratio = None if not vol_sma else round(last["v"] / vol_sma, 2)

    hh = False
    hl_swings = False
    if len(sh_ok) >= 2:
        hh = completed[sh_ok[-1]]["h"] > completed[sh_ok[-2]]["h"]
    if len(sl_ok) >= 2:
        hl_swings = completed[sl_ok[-1]]["l"] > completed[sl_ok[-2]]["l"]
    lows = [c["l"] for c in completed[-6:]]
    closes = [c["c"] for c in completed[-6:]]
    hl_short = len(lows) >= 5 and lows[-3] > lows[-5] and lows[-1] > lows[-4] and closes[-1] > closes[-3]
    hl = bool(hl_swings or hl_short)

    bh = False
    break_unconfirmed = False
    weak_hold = False
    thin_vol = False
    if close > rh and rng > 0 and break_pct is not None and break_pct >= 0.25:
        break_unconfirmed = True
        prev_beyond = len(completed) >= 2 and completed[-2]["c"] > rh
        held = last["l"] >= rh * 0.997
        two_closes = prev_beyond and close > rh
        if two_closes and held:
            bh = True
            break_unconfirmed = False
        elif two_closes and last["l"] >= rh * 0.992:
            weak_hold = True
            bh = False
            break_unconfirmed = True
        elif close > rh and not prev_beyond:
            break_unconfirmed = True
            bh = False
        if bh and vol_ratio is not None and vol_ratio < 0.7:
            thin_vol = True
            bh = False
            break_unconfirmed = True
        if bh and shp["up_wick"] >= 0.55 and shp["close_loc"] < 55:
            weak_hold = True
            bh = False
            break_unconfirmed = True

    fb = (last["h"] > rh and last["c"] < rh) or (last["l"] < rl and last["c"] > rl)
    if (not fb) and last["h"] >= rh * 0.999 and shp["up_wick"] >= 0.45 and last["c"] < rh and shp["close_loc"] < 60:
        fb = True

    pullback = pr_struct is not None and pr_struct < 80
    return {
        "ok": True,
        "n": len(cs),
        "n_completed": len(completed),
        "bh": bh,
        "break_unconfirmed": break_unconfirmed,
        "fb": fb,
        "hl": hl,
        "pullback": pullback,
        "rh": rh,
        "rl": rl,
        "pr_struct": None if pr_struct is None else round(pr_struct, 1),
        "break_pct": None if break_pct is None else round(break_pct, 3),
        "last_close": close,
        "last_high": last["h"],
        "forming_close": None if not forming else forming["c"],
        "forming_high": None if not forming else forming["h"],
        "forming": forming is not None,
        "atr": None if atr is None else round(atr, 8),
        "vol_ratio": vol_ratio,
        "up_wick": round(shp["up_wick"], 3),
        "dn_wick": round(shp["dn_wick"], 3),
        "close_loc": round(shp["close_loc"], 1),
        "hh": hh,
        "hl_swings": hl_swings,
        "n_sh": len(sh_ok),
        "n_sl": len(sl_ok),
        "weak_hold": weak_hold,
        "thin_vol": thin_vol,
    }

print("load prev state")
prev = {}
if STATE.exists():
    prev = load_state(STATE)
prev_struct = {x["s"]: x for x in (prev.get("struct80") or []) if isinstance(x, dict) and "s" in x}
prev_book = prev.get("candidate_book") or {}
ohlc_cache = prev.get("ohlc_cache") if isinstance(prev.get("ohlc_cache"), dict) else {}

print("tickers")
tick = curl_json("https://www.okx.com/api/v5/market/tickers?instType=SPOT")
if tick.get("code") != "0":
    raise SystemExit(f"tickers fail {tick}")

rows = []
n_usdt = 0
n_skip = 0
for x in tick["data"]:
    inst = x.get("instId") or ""
    if not inst.endswith("-USDT"):
        continue
    n_usdt += 1
    base = inst[:-5]
    if is_skip_base(base):
        n_skip += 1
        continue
    rows.append({
        "s": base,
        "inst": inst,
        "p": float(x["last"]),
        "o": float(x["open24h"]),
        "h": float(x["high24h"]),
        "l": float(x["low24h"]),
        "v": float(x.get("volCcy24h") or 0),
        "c": chg_pct(x["last"], x["open24h"]),
        "pr": pr_in_range(x["last"], x["high24h"], x["low24h"]),
    })
rows.sort(key=lambda r: r["v"], reverse=True)
top80 = rows[:80]
btc = next(r for r in rows if r["s"] == "BTC")
btc_chg = btc["c"] or 0.0
for r in top80:
    r["rs"] = None if r["c"] is None else round(r["c"] - btc_chg, 2)

print(f"usdt={n_usdt} skip={n_skip} nonstable={len(rows)} top80={len(top80)} BTC={btc['p']}")

okx_btc = btc["p"]
mexc = curl_json("https://api.mexc.com/api/v3/ticker/price?symbol=BTCUSDT")
gate = curl_json("https://api.gateio.ws/api/v4/spot/tickers?currency_pair=BTC_USDT")
mexc_btc = fnum(mexc.get("price")) if isinstance(mexc, dict) else None
gate_btc = None
if isinstance(gate, list) and gate:
    gate_btc = fnum(gate[0].get("last"))
prices = [p for p in (okx_btc, mexc_btc, gate_btc) if p]
med = statistics.median(prices) if prices else okx_btc
sanity = {}
for name, p in (("OKX", okx_btc), ("MEXC", mexc_btc), ("Gate", gate_btc)):
    sanity[name] = None if p is None else {"p": p, "diff_pct": round(100 * (p / med - 1), 4)}
flag = any(abs(v["diff_pct"]) > 0.3 for v in sanity.values() if v)
print("sanity", sanity, "flag", flag)

fund = curl_json("https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP")
oi = curl_json("https://www.okx.com/api/v5/public/open-interest?instId=BTC-USDT-SWAP")
btc_funding = None
oi_usd = None
try:
    btc_funding = float(fund["data"][0]["fundingRate"])
except Exception:
    pass
try:
    oi_usd = float(oi["data"][0]["oiUsd"])
except Exception:
    pass
print("funding", btc_funding, "oi", oi_usd)

def get_candles(inst, bar, limit):
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst}&bar={bar}&limit={limit}"
    return parse_candles(curl_json(url, retries=5, sleep=0.4))

print("4H all 80")
struct = []
fail4 = []
live_4h = {}
for i, r in enumerate(top80):
    live = get_candles(r["inst"], "4H", 100)
    cached = ((ohlc_cache.get(r["s"]) or {}).get("4H") or [])
    cs = merge_candles(cached, live, 120)
    live_4h[r["s"]] = cs
    st = classify_tf(cs, 12)
    if not st["ok"]:
        fail4.append(r["s"])
    ext = (r["pr"] is not None and r["pr"] >= 85 and r["c"] is not None and r["c"] >= 8)
    why = "NO_A"
    if not st["ok"]:
        why = "MISSING_4H"
    elif ext:
        why = "EXT_BAN"
    elif st["fb"] and not st["bh"]:
        why = "FAILED_BREAK_TRAP"
    elif st["bh"]:
        why = "RANGE_BREAK_HOLD"
    elif st.get("thin_vol") or st.get("weak_hold") or st.get("break_unconfirmed"):
        why = "BREAK_UNCONFIRMED"
    elif st["hl"] and st["pullback"]:
        why = "HL_CONTINUATION"
    elif st["hl"] and not st["pullback"]:
        why = "HL_NO_PULLBACK"
    prev_r = prev_struct.get(r["s"])
    vol_delta = None
    if prev_r and prev_r.get("v"):
        try:
            vol_delta = round(100.0 * (r["v"] / float(prev_r["v"]) - 1), 2)
        except Exception:
            vol_delta = None
    pr_delta = None
    if prev_r and prev_r.get("pr") is not None and r["pr"] is not None:
        pr_delta = round(r["pr"] - float(prev_r["pr"]), 1)
    why_prev = (prev_r or {}).get("why")
    item = {
        **r,
        "why": why,
        "why_prev": why_prev,
        "bh4": st["bh"],
        "fb4": st["fb"],
        "hl4": st["hl"],
        "rh4": st["rh"],
        "rl4": st["rl"],
        "pr4": st["pr_struct"],
        "break_pct4": st["break_pct"],
        "break_unconfirmed": st.get("break_unconfirmed"),
        "n4": st["n"],
        "n4c": st.get("n_completed"),
        "forming4": st["forming"],
        "form_c4": st["forming_close"],
        "vol_delta": vol_delta,
        "pr_delta": pr_delta,
        "ext": ext,
        "atr4": st.get("atr"),
        "volr4": st.get("vol_ratio"),
        "wick_up4": st.get("up_wick"),
        "wick_dn4": st.get("dn_wick"),
        "cloc4": st.get("close_loc"),
        "hh4": st.get("hh"),
        "hlsw4": st.get("hl_swings"),
        "weak_hold": st.get("weak_hold"),
        "thin_vol": st.get("thin_vol"),
    }
    struct.append(item)
    if i % 10 == 9:
        print(f"  4H {i+1}/80")

print("4h_ok", sum(1 for x in struct if (x.get("n4c") or x["n4"]) >= 12), "fail4", fail4)

rs_vals = [x["rs"] for x in struct if x["rs"] is not None]
rs_med = statistics.median(rs_vals) if rs_vals else 0
isolated = []
for x in struct:
    if x["c"] is None or x["rs"] is None:
        continue
    if x["c"] >= 8 and x["rs"] >= 6 and x["rs"] >= (rs_med + 5):
        isolated.append(x["s"])
        if x["why"] in ("RANGE_BREAK_HOLD", "HL_CONTINUATION", "BREAK_UNCONFIRMED"):
            x["why"] = "ISOLATED_PUMP"

prio = {
    "RANGE_BREAK_HOLD": 0,
    "BREAK_UNCONFIRMED": 1,
    "HL_CONTINUATION": 2,
    "FAILED_BREAK_TRAP": 3,
    "HL_NO_PULLBACK": 4,
}
cands = []
for x in struct:
    book = prev_book.get(x["s"]) or {}
    evolving = book.get("status") in ("watch", "break_unconfirmed", "hl_cont", "a")
    if x["why"] in prio or x.get("break_unconfirmed") or (x.get("pr") or 0) >= 88 or evolving:
        cands.append(x)
cands.sort(key=lambda x: (prio.get(x["why"], 9), -(x.get("pr") or 0)))
cands = cands[:25]
print("1H candidates", [x["s"] for x in cands], "n", len(cands))

fail1 = []
live_1h = {}
for x in cands:
    live = get_candles(x["inst"], "1H", 100)
    cached = ((ohlc_cache.get(x["s"]) or {}).get("1H") or [])
    cs1 = merge_candles(cached, live, 120)
    live_1h[x["s"]] = cs1
    st1 = classify_tf(cs1, 12)
    x["n1"] = st1["n"]
    x["bh1"] = st1["bh"]
    x["fb1"] = st1["fb"]
    x["hl1"] = st1["hl"]
    x["rh1"] = st1["rh"]
    x["rl1"] = st1["rl"]
    x["pr1"] = st1["pr_struct"]
    x["break_unconfirmed1"] = st1.get("break_unconfirmed")
    x["forming1"] = st1["forming"]
    x["volr1"] = st1.get("vol_ratio")
    x["cloc1"] = st1.get("close_loc")
    if not st1["ok"]:
        fail1.append(x["s"])
    if x["why"] == "RANGE_BREAK_HOLD" and st1["fb"]:
        x["why"] = "1H_FAILED_HOLD"
    if x["why"] == "RANGE_BREAK_HOLD" and st1.get("thin_vol") and not st1["bh"]:
        x["why"] = "BREAK_UNCONFIRMED"

print("1h_ok", len(cands) - len(fail1), "fail1", fail1)

live_1d_btc = get_candles("BTC-USDT", "1D", 60) or []
btc_1d = classify_tf(merge_candles(((ohlc_cache.get("BTC") or {}).get("1D") or []), live_1d_btc, 80), 20)

# 15m confirm only on names that already have 4H structure interest.
# Forming 15m never creates A. Failed 15m hold demotes A.
live_15m = {}
confirm15_names = []
seen15 = set()
for x in struct:
    if x["why"] in ("RANGE_BREAK_HOLD", "BREAK_UNCONFIRMED", "HL_CONTINUATION") or x.get("break_unconfirmed"):
        if x["s"] not in seen15:
            confirm15_names.append(x)
            seen15.add(x["s"])
confirm15_names = confirm15_names[:10]
for x in confirm15_names:
    live = get_candles(x["inst"], "15m", 80)
    cached = ((ohlc_cache.get(x["s"]) or {}).get("15m") or [])
    cs15 = merge_candles(cached, live, 100)
    live_15m[x["s"]] = cs15
    st15 = classify_tf(cs15, 16)
    x["n15"] = st15["n"]
    x["bh15"] = st15["bh"]
    x["fb15"] = st15["fb"]
    x["hl15"] = st15["hl"]
    x["forming15"] = st15["forming"]
    x["volr15"] = st15.get("vol_ratio")
    if x["why"] == "RANGE_BREAK_HOLD" and st15["fb"] and not st15["bh"]:
        x["why"] = "15M_FAILED_HOLD"
    if x["why"] == "RANGE_BREAK_HOLD" and st15.get("thin_vol") and not st15["bh"]:
        x["why"] = "BREAK_UNCONFIRMED"

# Alt funding + OI on A/unconf/hl. Never used as lone entry.
# Persist oi_usd in alt_flow_mem so rotation of the live-12 does not wipe delta.
def swap_flow(base):
    inst = f"{base}-USDT-SWAP"
    fr = curl_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}", retries=3, sleep=0.25)
    oi_a = curl_json(f"https://www.okx.com/api/v5/public/open-interest?instId={inst}", retries=3, sleep=0.25)
    fund = None
    oi_u = None
    try:
        fund = float(fr["data"][0]["fundingRate"])
    except Exception:
        pass
    try:
        oi_u = float(oi_a["data"][0]["oiUsd"])
    except Exception:
        pass
    return fund, oi_u

def oi_hist_prev(base):
    inst = f"{base}-USDT-SWAP"
    h = curl_json(
        f"https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-history?instId={inst}&period=1H&limit=3",
        retries=2, sleep=0.25
    )
    try:
        rows = h.get("data") or []
        if len(rows) >= 2:
            return float(rows[1][3])
        if len(rows) == 1:
            return float(rows[0][3])
    except Exception:
        return None
    return None

def pct_oi(now_v, prev_v):
    try:
        now_v = float(now_v)
        prev_v = float(prev_v)
        if prev_v <= 0 or now_v <= 0:
            return None
        return round(100.0 * (now_v / prev_v - 1), 3)
    except Exception:
        return None

alt_flow_mem = {}
if isinstance(prev.get("alt_flow_mem"), dict):
    for k, v in prev["alt_flow_mem"].items():
        if isinstance(v, dict) and v.get("oi_usd"):
            alt_flow_mem[k] = {
                "oi_usd": v.get("oi_usd"),
                "funding": v.get("funding"),
                "t": v.get("t"),
            }
for k, v in (prev.get("alt_flow") or {}).items():
    if isinstance(v, dict) and v.get("oi_usd") and k not in alt_flow_mem:
        alt_flow_mem[k] = {
            "oi_usd": v.get("oi_usd"),
            "funding": v.get("funding"),
            "t": prev.get("updated"),
        }

alt_flow = {}
flow_targets = []
seenf = set()
for x in struct:
    if x["why"] in ("RANGE_BREAK_HOLD", "BREAK_UNCONFIRMED", "HL_CONTINUATION", "15M_FAILED_HOLD", "1H_FAILED_HOLD"):
        if x["s"] not in seenf:
            flow_targets.append(x["s"])
            seenf.add(x["s"])
for name in ["ETH", "SOL", "BTC"] + flow_targets:
    if name in alt_flow or len(alt_flow) >= 12:
        continue
    fund, oi_u = swap_flow(name)
    prev_oi_a = (alt_flow_mem.get(name) or {}).get("oi_usd")
    dlt = pct_oi(oi_u, prev_oi_a) if oi_u and prev_oi_a else None
    if dlt is None and oi_u:
        hist_prev = oi_hist_prev(name)
        dlt = pct_oi(oi_u, hist_prev)
    alt_flow[name] = {"funding": fund, "oi_usd": oi_u, "oi_delta": dlt}
    if oi_u:
        alt_flow_mem[name] = {"oi_usd": oi_u, "funding": fund, "t": now_cest().isoformat()}
    row = next((z for z in struct if z["s"] == name), None)
    if row:
        row["alt_funding"] = fund
        row["alt_oi_usd"] = oi_u
        row["alt_oi_delta"] = dlt
if len(alt_flow_mem) > 80:
    keep = set(alt_flow.keys())
    items = sorted(
        alt_flow_mem.items(),
        key=lambda kv: (0 if kv[0] in keep else 1, str(kv[1].get("t") or "")),
        reverse=True,
    )
    alt_flow_mem = dict(items[:80])
print("alt_flow", {k: v for k, v in alt_flow.items() if v.get("funding") is not None or v.get("oi_usd")})

def px_round(p):
    if p is None:
        return None
    p = float(p)
    if p >= 1000:
        return round(p, 1)
    if p >= 100:
        return round(p, 2)
    if p >= 1:
        return round(p, 4)
    if p >= 0.01:
        return round(p, 6)
    return round(p, 8)

def parse_command(text):
    """Parse KOMMANDO-line or last_action into a structured fill."""
    if not text or not isinstance(text, str):
        return None
    t = text.strip()
    if t.startswith("KOMMANDO:"):
        t = t.split(":", 1)[1].strip()
    parts = t.replace(",", " ").split()
    if not parts:
        return None
    op = parts[0].upper()
    if op == "CASH":
        return {"op": "CASH"}
    if op in ("KÖP", "KOP", "BUY") and len(parts) >= 2:
        out = {"op": "KÖP", "s": parts[1].upper().replace("USDT", "")}
        kv = {}
        i = 2
        if i < len(parts) and parts[i].endswith("%"):
            try:
                out["size_pct"] = float(parts[i].replace("%", ""))
            except Exception:
                pass
            i += 1
        while i < len(parts) - 1:
            key = parts[i].lower()
            val = parts[i + 1]
            if key in ("entry", "stop", "tp1", "tp2"):
                fv = fnum(val)
                if fv is not None:
                    out[key] = fv
            i += 2
        return out
    if op in ("ADD",) and len(parts) >= 2:
        out = {"op": "ADD", "s": parts[1].upper().replace("USDT", "")}
        if len(parts) >= 3 and parts[2].endswith("%"):
            out["size_pct"] = fnum(parts[2].replace("%", ""))
        return out
    if op in ("SÄLJ", "SALJ", "SELL") and len(parts) >= 2:
        out = {"op": "SÄLJ", "s": parts[1].upper().replace("USDT", ""), "frac": 1.0}
        up = [p.upper() for p in parts]
        if "HÄLFTEN" in up or "HALFTEN" in up:
            out["frac"] = 0.5
        if "till" in [p.lower() for p in parts]:
            try:
                idx = [p.lower() for p in parts].index("till")
                out["exit"] = fnum(parts[idx + 1])
            except Exception:
                pass
        return out
    if op in ("HÅLL", "HALL", "HOLD") and len(parts) >= 2:
        return {"op": "HÅLL", "s": parts[1].upper().replace("USDT", "")}
    return {"op": "OTHER", "raw": t}


def _norm_cmd(cmd, state=None):
    """Accept both engine schemas: op/s and cmd/token."""
    if not isinstance(cmd, dict):
        return None
    out = dict(cmd)
    op = out.get("op") or out.get("cmd") or out.get("action")
    if isinstance(op, str):
        op = op.upper().replace("KOP", "KÖP").replace("BUY", "KÖP").replace("SELL", "SÄLJ").replace("SALJ", "SÄLJ")
        out["op"] = op
    sym = out.get("s") or out.get("token") or out.get("sym") or out.get("ticker")
    if isinstance(sym, str):
        out["s"] = sym.upper().replace("USDT", "")
    # Bare KÖP TOKEN without prices: take trade_plans / last_prices
    if out.get("op") == "KÖP" and out.get("s") and not out.get("entry"):
        plans = (state or {}).get("trade_plans") or {}
        plan = plans.get(out["s"]) or {}
        out["entry"] = out.get("entry") or plan.get("entry")
        out["stop"] = out.get("stop") or plan.get("stop")
        out["tp1"] = out.get("tp1") or plan.get("tp1")
        out["tp2"] = out.get("tp2") or plan.get("tp2")
        if not out.get("size_pct"):
            out["size_pct"] = plan.get("size_pct") or 45.0
    return out


def apply_pending_fills(state, price_now, ts):
    """Apply last_command / pending_fills once. Never double-buy or re-sell."""
    positions = list(state.get("positions") or [])
    cash = float(state.get("cash_sek") or 0)
    closed = list(state.get("closed_trades") or [])
    applied = []

    cmds = []
    if isinstance(state.get("pending_fills"), list):
        cmds.extend(state.get("pending_fills"))
    pc = state.get("pending_command") or state.get("last_command")
    if isinstance(pc, dict):
        cmds.append(pc)
    elif isinstance(pc, str):
        parsed = parse_command(pc)
        if parsed:
            cmds.append(parsed)
    # last_action KÖP/SÄLJ even without prices — prices filled from trade_plans
    la = parse_command(state.get("last_action"))
    if la and la.get("op") in ("KÖP", "SÄLJ", "ADD"):
        cmds.append(la)
    # History is an audit log, never an order queue. Replaying it could re-buy a
    # position after a later sell. New fills must come from an explicit command.
    cmds = [c for c in (_norm_cmd(c, state) for c in cmds) if c]

    held = {p.get("s") for p in positions if p.get("s")}
    total_guess = float(state.get("total_value_sek") or (cash + sum(float(p.get("alloc_sek") or 0) for p in positions)))

    def mark_px(sym, fallback=None):
        return fnum(price_now.get(sym)) or fnum(fallback)

    for cmd in cmds:
        if not isinstance(cmd, dict):
            continue
        op = cmd.get("op")
        sym = cmd.get("s")
        if op == "KÖP" and sym:
            if sym in held:
                applied.append({"skip": "already_held", "s": sym, "op": op})
                continue
            if len(positions) >= 2:
                applied.append({"skip": "max_pos", "s": sym, "op": op})
                continue
            entry = fnum(cmd.get("entry")) or mark_px(sym)
            if not entry:
                applied.append({"skip": "no_price", "s": sym, "op": op})
                continue
            size_pct = fnum(cmd.get("size_pct")) or 45.0
            size_pct = min(55.0, max(40.0, size_pct))
            alloc = round(total_guess * size_pct / 100.0, 2)
            if alloc > cash * 0.995:
                alloc = round(cash * 0.98, 2)
            if alloc < 50:
                applied.append({"skip": "no_cash", "s": sym, "op": op})
                continue
            qty = alloc / entry
            stop = fnum(cmd.get("stop"))
            start_k = float(state.get("start_kassa_sek") or total_guess or 10000)
            size_lock = round(100.0 * alloc / start_k, 2) if start_k else size_pct
            positions.append({
                "s": sym, "entry": entry, "stop": stop,
                "tp1": fnum(cmd.get("tp1")), "tp2": fnum(cmd.get("tp2")),
                "size_pct": size_lock,
                "size_pct_locked": size_lock,
                "size_pct_cmd": size_pct,
                "mtm_weight_pct": size_lock,
                "qty": qty, "alloc_sek": alloc,
                "opened": ts, "mark": entry,
                "fills": 1,
                "legs": [{"t": ts, "op": "KÖP", "px": entry, "qty": qty, "alloc_sek": alloc, "size_pct": size_lock}],
            })
            cash -= alloc
            held.add(sym)
            applied.append({"fill": "KÖP", "s": sym, "entry": entry, "alloc": alloc, "size_pct": size_lock})
        elif op == "ADD" and sym:
            pos = next((p for p in positions if p.get("s") == sym), None)
            if not pos:
                applied.append({"skip": "add_not_held", "s": sym, "op": op})
                continue
            entry = fnum(cmd.get("entry")) or mark_px(sym, pos.get("mark") or pos.get("entry"))
            if not entry:
                applied.append({"skip": "no_price", "s": sym, "op": op})
                continue
            add_pct = fnum(cmd.get("size_pct")) or 20.0
            add_pct = min(25.0, max(5.0, add_pct))
            start_k = float(state.get("start_kassa_sek") or total_guess or 10000)
            alloc = round(start_k * add_pct / 100.0, 2)
            if alloc > cash * 0.995:
                alloc = round(cash * 0.98, 2)
            if alloc < 50:
                applied.append({"skip": "no_cash", "s": sym, "op": op})
                continue
            qty_add = alloc / entry
            old_qty = float(pos.get("qty") or 0)
            old_alloc = float(pos.get("alloc_sek") or 0)
            new_qty = old_qty + qty_add
            new_alloc = old_alloc + alloc
            pos["entry"] = (float(pos.get("entry") or entry) * old_qty + entry * qty_add) / new_qty if new_qty else entry
            pos["qty"] = new_qty
            pos["alloc_sek"] = new_alloc
            pos["size_pct_locked"] = round(100.0 * new_alloc / start_k, 2) if start_k else add_pct
            pos["size_pct"] = pos["size_pct_locked"]
            pos["fills"] = int(pos.get("fills") or 1) + 1
            legs = list(pos.get("legs") or [])
            legs.append({"t": ts, "op": "ADD", "px": entry, "qty": qty_add, "alloc_sek": alloc, "size_pct": add_pct})
            pos["legs"] = legs
            if cmd.get("stop"):
                # never loosen
                old_stop = fnum(pos.get("stop"))
                new_stop = fnum(cmd.get("stop"))
                if old_stop is None or new_stop > old_stop:
                    pos["stop"] = new_stop
            cash -= alloc
            applied.append({"fill": "ADD", "s": sym, "entry": entry, "alloc": alloc, "size_pct": add_pct})
        elif op == "SÄLJ" and sym:
            pos = next((p for p in positions if p.get("s") == sym), None)
            if not pos:
                applied.append({"skip": "not_held", "s": sym, "op": op})
                continue
            frac = float(cmd.get("frac") or 1.0)
            px = fnum(cmd.get("exit")) or mark_px(sym, pos.get("mark") or pos.get("entry"))
            if not px:
                applied.append({"skip": "no_price", "s": sym, "op": op})
                continue
            qty = float(pos.get("qty") or 0) * frac
            proceeds = qty * px
            entry = float(pos.get("entry") or 0)
            pnl = (px - entry) * qty
            closed.append({
                "s": sym, "side": "LONG", "entry": entry, "exit": px,
                "qty": qty, "pnl_sek": round(pnl, 2),
                "pct": round(100.0 * (px / entry - 1), 3) if entry else None,
                "opened": pos.get("opened"), "closed": ts,
                "reason": cmd.get("anledning") or "SÄLJ",
            })
            cash += proceeds
            if frac >= 0.999:
                positions = [p for p in positions if p.get("s") != sym]
                held.discard(sym)
            else:
                pos["qty"] = float(pos.get("qty") or 0) * (1 - frac)
                pos["alloc_sek"] = float(pos.get("alloc_sek") or 0) * (1 - frac)
                pos["size_pct"] = float(pos.get("size_pct") or 0) * (1 - frac)
            applied.append({"fill": "SÄLJ", "s": sym, "exit": px, "pnl": round(pnl, 2), "frac": frac})

    state["positions"] = positions
    state["cash_sek"] = cash
    # normalize pnl field so wins/losses never go 0 pga pnl vs pnl_sek
    norm_closed = []
    for t in closed:
        if not isinstance(t, dict):
            continue
        row = dict(t)
        pnl = row.get("pnl_sek")
        if pnl is None:
            pnl = row.get("pnl")
        try:
            pnl = float(pnl)
        except Exception:
            pnl = 0.0
        row["pnl"] = round(pnl, 2)
        row["pnl_sek"] = round(pnl, 2)
        norm_closed.append(row)
    state["closed_trades"] = norm_closed[-200:]
    wins = sum(1 for t in state["closed_trades"] if (t.get("pnl_sek") or 0) > 0)
    losses = sum(1 for t in state["closed_trades"] if (t.get("pnl_sek") or 0) < 0)
    open_n = len(positions)
    state["wins"] = wins
    state["losses"] = losses
    state["open_trades"] = open_n
    state["trades"] = len(state["closed_trades"]) + open_n
    state["pending_fills"] = []
    state["pending_command"] = None
    state["last_command"] = None
    state["fills_applied"] = applied[-20:]
    return state


def reconcile_action(state):
    """Do not keep a stale KÖP/SÄLJ as last_action after it is already in the book."""
    held = [p.get("s") for p in (state.get("positions") or []) if p.get("s")]
    la = (state.get("last_action") or "CASH").strip()
    parsed = parse_command(la)
    op = (parsed or {}).get("op")
    sym = (parsed or {}).get("s")
    if op == "KÖP" and sym and sym in held:
        # already filled — scan tick should not look like a fresh buy
        pass
    if op == "SÄLJ" and sym and sym not in held:
        la = ("HÅLL " + " ".join(held)) if held else "CASH"
    if op == "KÖP" and sym and sym in held:
        # keep last_action as the fill label only if newest history does not already have it
        hist = state.get("history") or []
        already = any((h.get("action") or "").startswith("KÖP " + sym) for h in hist[-6:])
        if already:
            la = ("HÅLL " + " ".join(held)) if held else "CASH"
    if not held and op not in ("CASH", "SÄLJ"):
        la = "CASH"
    state["last_action"] = la
    return la, held


def plan_levels(x):
    """ATR stop/TP. Never creates a buy — only sizes risk if A already exists."""
    p = fnum(x.get("p"))
    atr = fnum(x.get("atr4"))
    rl = fnum(x.get("rl4"))
    rh = fnum(x.get("rh4"))
    if p is None:
        return None
    if atr is None or atr <= 0:
        atr = p * 0.018
    stop_atr = p - 1.4 * atr
    stop_struct = (rl * 0.997) if rl else None
    # use the HIGHER stop (tighter) that still sits below price
    cands_stop = [s for s in (stop_atr, stop_struct) if s and s < p * 0.998]
    stop = max(cands_stop) if cands_stop else p - 1.6 * atr
    risk = p - stop
    if risk <= 0:
        return None
    rr = risk / p
    # reject insane wide or microscopic stops
    if rr > 0.09 or rr < 0.004:
        stop = p - min(max(1.2 * atr, p * 0.012), p * 0.06)
        risk = p - stop
    tp1 = p + 1.6 * risk
    tp2 = p + 2.8 * risk
    if rh and rh > p:
        # if range high is close above, don't pretend TP is below structure
        pass
    return {
        "entry": px_round(p),
        "stop": px_round(stop),
        "tp1": px_round(tp1),
        "tp2": px_round(tp2),
        "atr4": atr,
        "risk_pct": round(100.0 * (p - stop) / p, 2),
        "rr1": 1.6,
        "rr2": 2.8,
    }

def quality(x):
    q = 0
    if x["why"] == "RANGE_BREAK_HOLD":
        q += 5
    if x["why"] == "HL_CONTINUATION":
        q += 3
    if x["why"] == "BREAK_UNCONFIRMED":
        q += 2
    if x.get("bh1"):
        q += 1
    if x.get("fb1"):
        q -= 2
    if x.get("bh15"):
        q += 1
    if x.get("fb15"):
        q -= 1
    if x.get("alt_oi_delta") is not None and x["alt_oi_delta"] > 1.5 and (x.get("alt_funding") or 0) < 0.0003:
        q += 1
    if x.get("alt_oi_delta") is not None and x["alt_oi_delta"] < -2:
        q -= 1
    if x.get("hlsw4"):
        q += 1
    if x.get("hh4"):
        q += 1
    if x.get("volr4") is not None and x["volr4"] >= 1.3:
        q += 1
    if x.get("vol_delta") is not None and x["vol_delta"] > 5:
        q += 1
    if x.get("vol_delta") is not None and x["vol_delta"] < -10:
        q -= 1
    if x.get("weak_hold") or x.get("thin_vol"):
        q -= 2
    if x.get("ext"):
        q -= 4
    if x["s"] in isolated:
        q -= 3
    return q

for x in struct:
    x["q"] = quality(x)
    prev_r = prev_struct.get(x["s"])
    x["q_prev"] = prev_r.get("q") if prev_r else None
    x["q_delta"] = None if x["q_prev"] is None else x["q"] - x["q_prev"]
    x["plan"] = plan_levels(x)

book = dict(prev_book)
ts = now_cest().isoformat()
active_why = {"RANGE_BREAK_HOLD", "BREAK_UNCONFIRMED", "HL_CONTINUATION", "FAILED_BREAK_TRAP", "HL_NO_PULLBACK", "1H_FAILED_HOLD", "15M_FAILED_HOLD"}
for x in struct:
    if x["why"] not in active_why and x["s"] not in book:
        continue
    b = book.get(x["s"]) or {"s": x["s"], "first": ts, "hist": []}
    b["last"] = ts
    b["why"] = x["why"]
    b["p"] = x["p"]
    b["pr"] = x["pr"]
    b["q"] = x["q"]
    b["vol_delta"] = x.get("vol_delta")
    b["atr4"] = x.get("atr4")
    b["volr4"] = x.get("volr4")
    if x.get("plan"):
        b["plan"] = x["plan"]
    b["status"] = {
        "RANGE_BREAK_HOLD": "a",
        "HL_CONTINUATION": "hl_cont",
        "BREAK_UNCONFIRMED": "break_unconfirmed",
        "FAILED_BREAK_TRAP": "watch",
        "HL_NO_PULLBACK": "watch",
        "1H_FAILED_HOLD": "watch",
        "15M_FAILED_HOLD": "watch",
    }.get(x["why"], "watch")
    b["hist"] = (b.get("hist") or [])[-10:] + [{
        "t": ts, "why": x["why"], "p": x["p"],
        "pr": None if x["pr"] is None else round(x["pr"], 1),
        "q": x["q"], "vol_delta": x.get("vol_delta"),
        "cloc4": x.get("cloc4"), "volr4": x.get("volr4"),
    }]
    b["improving"] = False
    if len(b["hist"]) >= 2:
        b["improving"] = (b["hist"][-1]["q"] or 0) > (b["hist"][-2]["q"] or 0)
    qs = [h.get("q") for h in b["hist"] if h.get("q") is not None]
    streak = 0
    for i in range(len(qs) - 1, 0, -1):
        if qs[i] > qs[i - 1]:
            streak += 1
        else:
            break
    b["q_streak"] = streak
    book[x["s"]] = b

a_setups = [x for x in struct if x["why"] == "RANGE_BREAK_HOLD"]
pinned = set(x["s"] for x in a_setups) | set(
    p.get("s") for p in ((prev.get("positions") if isinstance(prev, dict) else None) or []) if p.get("s")
)
if len(book) > 40:
    pinned_items = [(k, v) for k, v in book.items() if k in pinned]
    rest = [(k, v) for k, v in book.items() if k not in pinned]
    rest.sort(key=lambda kv: (kv[1].get("last") or "", kv[1].get("q") or 0), reverse=True)
    keep_n = max(40, len(pinned_items))
    book = dict(pinned_items + rest[: max(0, keep_n - len(pinned_items))])
hl_ok = [x for x in struct if x["why"] == "HL_CONTINUATION"]
unconf = [x for x in struct if x["why"] == "BREAK_UNCONFIRMED"]
near_a = [
    x for x in struct
    if x["why"] == "BREAK_UNCONFIRMED"
    and not x.get("ext")
    and x["s"] not in isolated
    and (x.get("q_delta") or 0) >= 0
    and (x.get("volr4") is None or x.get("volr4") >= 0.9)
][:8]

majors = [x["s"] for x in struct[:5]]
maj_chg = [x["c"] for x in struct[:5] if x["c"] is not None]
maj_up = sum(1 for c in maj_chg if c > 0)

fng = curl_json("https://api.alternative.me/fng/?limit=1")
fng_val = None
fng_cls = None
try:
    fng_val = int((fng.get("data") or [{}])[0].get("value"))
    fng_cls = (fng.get("data") or [{}])[0].get("value_classification")
except Exception:
    pass
cg = curl_json("https://api.coingecko.com/api/v3/global", retries=8, sleep=0.8)
btc_dom = None
btc_dom_src = None
try:
    btc_dom = float(((cg.get("data") or {}).get("market_cap_percentage") or {}).get("btc"))
    btc_dom_src = "coingecko"
except Exception:
    pass
if btc_dom is None:
    pap = curl_json("https://api.coinpaprika.com/v1/global", retries=4, sleep=0.4)
    try:
        btc_dom = float(pap.get("bitcoin_dominance_percentage"))
        btc_dom_src = "coinpaprika"
    except Exception:
        pass
if btc_dom is None:
    try:
        prev_dom = prev.get("btc_dom") if isinstance(prev, dict) else None
        if prev_dom is not None:
            btc_dom = float(prev_dom)
            btc_dom_src = "prev_state"
    except Exception:
        pass
if btc_dom is None:
    err = None
    if isinstance(cg, dict):
        err = cg.get("_error")
    print("btc_dom SAKNAS", str(err)[:160] if err else "no_source")
else:
    print("btc_dom", round(btc_dom, 3), btc_dom_src)

prev_oi = None
try:
    prev_oi = ((prev.get("snapshots") or [{}])[-1] or {}).get("oi_usd")
    if prev_oi is None:
        prev_oi = ((prev.get("btc_flow") or {}) if isinstance(prev.get("btc_flow"), dict) else {}).get("oi_usd")
except Exception:
    prev_oi = None
oi_delta = None
if oi_usd is not None and prev_oi:
    try:
        oi_delta = round(100.0 * (float(oi_usd) / float(prev_oi) - 1), 3)
    except Exception:
        oi_delta = None

btc_why = next((x["why"] for x in struct if x["s"] == "BTC"), "NO_A")
if btc_1d.get("bh"):
    regime = "TREND_UP"
elif btc_why == "RANGE_BREAK_HOLD":
    regime = "TREND_UP"
elif btc_why in ("FAILED_BREAK_TRAP", "BREAK_UNCONFIRMED"):
    regime = "RANGE"
elif btc_chg is not None and btc_chg <= -2:
    regime = "RISK_OFF"
else:
    regime = "RANGE"

if btc_chg is not None and btc_chg > 0.4 and maj_up >= 3:
    flow = "RISK_ON"
elif btc_chg is not None and btc_chg < -0.4 and maj_up <= 2:
    flow = "RISK_OFF"
else:
    flow = "MIXED"
flow_parts = []
if oi_delta is not None:
    flow_parts.append(f"OI{oi_delta:+.2f}%")
if btc_funding is not None:
    flow_parts.append(f"FUND{btc_funding*100:+.4f}%")
if fng_val is not None:
    flow_parts.append(f"FNG{fng_val}")
if btc_dom is not None:
    flow_parts.append(f"DOM{btc_dom:.1f}")
flow_detail = flow + (("|" + "|".join(flow_parts)) if flow_parts else "")

exp = prev.get("expectancy_log") or {"by_why": {}, "rows": [], "updated": ts, "note": "tick-vs-book.p"}
if "by_why" not in exp:
    exp["by_why"] = {}
if "rows" not in exp:
    exp["rows"] = []
price_now = {x["s"]: x["p"] for x in struct}
price_now.update({x["s"]: x["p"] for x in rows})
for name, entry in list(prev_book.items()):
    if not isinstance(entry, dict):
        continue
    p0 = entry.get("p")
    lastp = price_now.get(name)
    if not p0 or not lastp or float(p0) <= 0:
        continue
    outcome_pct = round(100.0 * (float(lastp) / float(p0) - 1), 3)
    if outcome_pct > 0.5:
        verdict = "up"
    elif outcome_pct < -0.5:
        verdict = "dn"
    else:
        verdict = "flat"
    why = str(entry.get("why") or "UNK")
    if why not in exp["by_why"]:
        exp["by_why"][why] = {"n": 0, "up": 0, "dn": 0, "flat": 0, "sum": 0.0, "avg": 0.0}
    bw = exp["by_why"][why]
    bw["n"] = int(bw.get("n") or 0) + 1
    bw["sum"] = float(bw.get("sum") or 0) + outcome_pct
    bw["avg"] = round(bw["sum"] / bw["n"], 3)
    bw[verdict] = int(bw.get(verdict) or 0) + 1
    exp["rows"].append({"t": ts, "s": name, "why": why, "p": p0, "last": lastp, "out": outcome_pct, "v": verdict})
    if name in book and isinstance(book[name], dict):
        book[name]["outcome_pct"] = outcome_pct
        book[name]["outcome"] = verdict
        book[name]["last_px"] = lastp
exp["rows"] = exp["rows"][-300:]
exp["updated"] = ts
exp["note"] = "outcome = last/prev_book.p-1 mellan körningar. Inte realiserad trade-PnL. Trösklar låsta. A-setups pinnas i book."
for _why in ("RANGE_BREAK_HOLD", "BREAK_UNCONFIRMED", "HL_CONTINUATION", "FAILED_BREAK_TRAP", "HL_NO_PULLBACK"):
    if _why not in exp["by_why"]:
        exp["by_why"][_why] = {"n": 0, "up": 0, "dn": 0, "flat": 0, "sum": 0.0, "avg": 0.0}

keep_names = set(["BTC", "ETH", "SOL"] + list(book.keys()) + [x["s"] for x in a_setups + unconf + hl_ok + cands])
new_cache = {}
for name in keep_names:
    old = ohlc_cache.get(name) or {}
    bars4 = live_4h.get(name)
    bars1 = live_1h.get(name)
    rec = {}
    if bars4:
        rec["4H"] = [compact_bar(c) for c in bars4 if str(c.get("confirm")) == "1"][-80:]
    elif old.get("4H"):
        rec["4H"] = old["4H"][-80:]
    if bars1:
        rec["1H"] = [compact_bar(c) for c in bars1 if str(c.get("confirm")) == "1"][-80:]
    elif old.get("1H"):
        rec["1H"] = old["1H"][-80:]
    bars15 = live_15m.get(name)
    if bars15:
        rec["15m"] = [compact_bar(c) for c in bars15 if str(c.get("confirm")) == "1"][-80:]
    elif old.get("15m"):
        rec["15m"] = old["15m"][-80:]
    if name == "BTC" and live_1d_btc:
        rec["1D"] = [compact_bar(c) for c in live_1d_btc if str(c.get("confirm")) == "1"][-60:]
    elif name == "BTC" and old.get("1D"):
        rec["1D"] = old["1D"][-60:]
    if rec:
        new_cache[name] = rec

out = {
    "ts": ts,
    "engine": "v3.3.3-depth",
    "btc": {"p": okx_btc, "c": btc_chg, "pr": btc["pr"], "funding": btc_funding, "oi_usd": oi_usd,
            "why": btc_why, "d1_bh": btc_1d.get("bh"), "d1_hl": btc_1d.get("hl")},
    "sanity": {"raw": sanity, "flag": flag, "median": med},
    "n_usdt": n_usdt,
    "n_skip": n_skip,
    "n_universe": len(rows),
    "n_4h": sum(1 for x in struct if (x.get("n4c") or 0) >= 12 or x["n4"] >= 12),
    "fail4": fail4,
    "n_1h": len(cands),
    "fail1": fail1,
    "cands_1h": [x["s"] for x in cands],
    "a_setups": [{"s": x["s"], "p": x["p"], "c": x["c"], "pr": x["pr"], "rs": x["rs"], "why": x["why"],
                  "rh4": x["rh4"], "q": x["q"], "atr4": x.get("atr4"), "volr4": x.get("volr4"),
                  "plan": x.get("plan"),
                  "improving": (book.get(x["s"]) or {}).get("improving")}
                 for x in a_setups],
    "hl_cont": [x["s"] for x in hl_ok],
    "unconfirmed": [x["s"] for x in unconf],
    "near_a": [{"s": x["s"], "p": x["p"], "q": x.get("q"), "q_delta": x.get("q_delta"),
                "why": x["why"], "volr4": x.get("volr4"), "bh15": x.get("bh15"), "fb15": x.get("fb15"),
                "alt_funding": x.get("alt_funding"), "alt_oi_delta": x.get("alt_oi_delta")}
               for x in near_a],
    "hl_raw": [x["s"] for x in struct if x["why"] == "HL_NO_PULLBACK"],
    "ext_ban": [x["s"] for x in struct if x["why"] == "EXT_BAN"],
    "fb": [x["s"] for x in struct if x["why"] == "FAILED_BREAK_TRAP"],
    "isolated": isolated,
    "majors_cohort": majors,
    "majors_up": maj_up,
    "rs_med": rs_med,
    "improving": [s for s, b in book.items() if b.get("improving")],
    "top12": [{"s": x["s"], "p": x["p"], "c": None if x["c"] is None else round(x["c"], 2),
               "pr": None if x["pr"] is None else round(x["pr"], 1), "rs": x["rs"],
               "why": x["why"], "vd": x.get("vol_delta")} for x in struct[:12]],
    "struct_min": [{
        "s": x["s"], "p": x["p"], "c": x["c"], "rs": x["rs"], "pr": x["pr"], "v": x["v"],
        "why": x["why"], "why_prev": x.get("why_prev"),
        "bh4": x["bh4"], "hl4": x["hl4"], "fb4": x["fb4"],
        "rh4": x.get("rh4"), "rl4": x.get("rl4"), "pr4": x.get("pr4"),
        "n4": x["n4"], "n4c": x.get("n4c"), "n1": x.get("n1"), "bh1": x.get("bh1"), "fb1": x.get("fb1"),
        "vol_delta": x.get("vol_delta"), "pr_delta": x.get("pr_delta"),
        "q": x["q"], "q_delta": x.get("q_delta"),
        "break_unconfirmed": x.get("break_unconfirmed"),
        "atr4": x.get("atr4"), "volr4": x.get("volr4"),
        "wick_up4": x.get("wick_up4"), "cloc4": x.get("cloc4"),
        "hh4": x.get("hh4"), "hlsw4": x.get("hlsw4"),
    } for x in struct],
    "candidate_book": book,
    "regime": regime,
    "flow": flow,
    "flow_detail": flow_detail,
    "fng": {"v": fng_val, "cls": fng_cls},
    "btc_dom": btc_dom,
    "oi_delta": oi_delta,
}
atomic_json_write(OUT, out)

state = dict(prev) if isinstance(prev, dict) else {}
state["version"] = "3.3.3"
state["updated"] = ts
state["last_action"] = prev.get("last_action") or "CASH"
state["cash_sek"] = prev.get("cash_sek", 10000)
state["positions"] = list(prev.get("positions") or [])
state["start_kassa_sek"] = prev.get("start_kassa_sek", 10000)
if not isinstance(state.get("closed_trades"), list):
    state["closed_trades"] = []
state = apply_pending_fills(state, price_now, ts)
mtm = float(state["cash_sek"] or 0)
for pos in state["positions"]:
    px = price_now.get(pos.get("s"))
    if px and pos.get("qty"):
        mtm += float(pos["qty"]) * float(px)
        pos["mark"] = px
        pos["value_sek"] = round(float(pos["qty"]) * float(px), 2)
    elif pos.get("value_sek"):
        mtm += float(pos["value_sek"])
state["total_value_sek"] = round(mtm, 2)
start = float(state.get("start_kassa_sek") or 10000)
for pos in state["positions"]:
    # size_pct = låst köpstorlek mot startkassa. MTM-vikt är separat och är INTE ett nytt köp.
    alloc = float(pos.get("alloc_sek") or 0)
    locked = pos.get("size_pct_locked")
    if locked is None:
        locked = round(100.0 * alloc / start, 2) if start else float(pos.get("size_pct") or 0)
    pos["size_pct_locked"] = locked
    pos["size_pct"] = locked
    if mtm > 0 and pos.get("value_sek") is not None:
        pos["mtm_weight_pct"] = round(100.0 * float(pos["value_sek"]) / mtm, 2)
    else:
        pos["mtm_weight_pct"] = locked
    if not pos.get("legs"):
        pos["legs"] = [{
            "t": pos.get("opened"), "op": "KÖP",
            "px": pos.get("entry"), "qty": pos.get("qty"),
            "alloc_sek": alloc, "size_pct": locked,
        }]
        pos["fills"] = 1
    else:
        pos["fills"] = len(pos.get("legs") or [])
    pos["display"] = (
        f"{pos.get('s')} fills={pos.get('fills')} "
        f"size_lock {locked}% @ {pos.get('entry')} "
        f"mtm_vikt {pos.get('mtm_weight_pct')}% "
        f"(mtm_vikt är INTE extra köp)"
    )
state["total_pnl_sek"] = round(mtm - start, 2)
state["total_pnl_pct"] = round(100.0 * (mtm / start - 1), 3) if start else 0
hw = float(state.get("high_water_sek") or start)
if mtm > hw:
    hw = mtm
state["high_water_sek"] = hw
state["max_drawdown_pct"] = round(min(float(state.get("max_drawdown_pct") or 0), 100.0 * (mtm / hw - 1)), 3) if hw else 0
state["regime"] = regime
state["flow"] = flow
state["flow_detail"] = flow_detail
state["fng"] = {"v": fng_val, "cls": fng_cls}
state["btc_dom"] = btc_dom
state["btc_flow"] = {"p": okx_btc, "c": btc_chg, "funding": btc_funding, "oi_usd": oi_usd, "oi_delta": oi_delta, "why": btc_why, "d1_bh": btc_1d.get("bh")}
state["funding"] = btc_funding
state["oi"] = oi_usd
state["oi_usd"] = oi_usd
state["oi_delta"] = oi_delta
state["process_note"] = "Efter varje scan: ersätt samma permanenta HELA statefil (struct80+candidate_book+expectancy_log). Autokör högst varje timme 24h Europe/Stockholm. Historik är aldrig orderkö."
lp = {x["s"]: x["p"] for x in struct}
lp.update({k: v for k, v in price_now.items() if v})
for pos in state.get("positions") or []:
    if pos.get("s") and pos.get("s") not in lp and pos.get("mark"):
        lp[pos["s"]] = pos["mark"]
state["last_prices"] = lp
state["universe"] = [x["s"] for x in cands]
state["watchlist"] = out["improving"]
state["struct80"] = out["struct_min"]
state["candidate_book"] = book
state["expectancy_log"] = exp
state["ohlc_cache"] = new_cache
state["engine"] = "v3.3.4-displaylock"
state["version"] = "3.3.4"
state["alt_flow"] = alt_flow
state["alt_flow_mem"] = alt_flow_mem
state["near_a"] = out.get("near_a") or []
state["scan80_summary"] = {
    "n_struct": len(struct), "n_4h": out["n_4h"], "n_1h": out["n_1h"],
    "fail4": fail4, "fail1": fail1, "a_setups": [x["s"] for x in a_setups],
    "unconfirmed": [x["s"] for x in unconf], "hl_cont": [x["s"] for x in hl_ok],
    "cands_1h": out["cands_1h"], "isolated": isolated, "ext_ban": out["ext_ban"],
    "near_a": [x["s"] for x in near_a],
}
snaps = state.get("snapshots") or []
snaps.append({
    "t": ts, "btc": okx_btc, "n_4h": out["n_4h"], "n_1h": out["n_1h"],
    "a": len(a_setups), "improving": out["improving"], "regime": regime,
    "flow": flow, "oi_usd": oi_usd, "funding": btc_funding, "fng": fng_val, "btc_dom": btc_dom,
    "sanity": {"flag": flag, "median": med}, "engine": "v3.3.3-depth",
    "near_a": [x["s"] for x in near_a],
})
state["snapshots"] = snaps[-60:]
if a_setups:
    plans = []
    for x in a_setups:
        pl = x.get("plan") or {}
        plans.append(f"{x['s']} entry {pl.get('entry')} stop {pl.get('stop')} tp1 {pl.get('tp1')} tp2 {pl.get('tp2')} risk {pl.get('risk_pct')}%")
    state["open_plan"] = "A: " + " | ".join(plans)
else:
    state["open_plan"] = "CASH. Inget A."
state["trade_plans"] = {x["s"]: x.get("plan") for x in a_setups if x.get("plan")}
# trail + exit engine. Freeze original TP. Never loosen stop. Lock profit.
KILL_WHY = {
    "FAILED_BREAK_TRAP", "EXT_BAN", "ISOLATED", "ISOLATED_PUMP",
    "1H_FAILED_HOLD", "15M_FAILED_HOLD",
}
pos_plans = {}
exit_advice = []
for pos in state.get("positions") or []:
    name = pos.get("s")
    row = next((x for x in struct if x["s"] == name), None)
    px = fnum((row or {}).get("p")) or fnum(price_now.get(name)) or fnum(pos.get("mark"))
    entry = fnum(pos.get("entry"))
    old_stop = fnum(pos.get("stop"))
    atr = fnum((row or {}).get("atr4")) if row else None
    if atr is None or atr <= 0:
        atr = (entry or px or 0) * 0.018
    why = (row or {}).get("why") or ""
    q = fnum((row or {}).get("q")) or 0
    if px:
        pos["mark"] = px
        prev_mfe = fnum(pos.get("mfe_px")) or entry or px
        pos["mfe_px"] = max(prev_mfe, px)
    mfe = fnum(pos.get("mfe_px")) or px or entry
    risk = None
    if entry and old_stop and entry > old_stop:
        risk = entry - old_stop
    elif entry and atr:
        risk = 1.4 * atr
    r_now = ((px - entry) / risk) if (px and entry and risk and risk > 0) else 0.0
    r_mfe = ((mfe - entry) / risk) if (mfe and entry and risk and risk > 0) else 0.0
    pos["r_now"] = round(r_now, 3)
    pos["r_mfe"] = round(r_mfe, 3)
    # freeze original targets
    if not pos.get("tp1") and entry and risk:
        pos["tp1"] = px_round(entry + 1.6 * risk)
        pos["tp2"] = px_round(entry + 2.8 * risk)
    # trail candidates (all must be below last)
    cands = [s for s in (old_stop,) if s]
    if px and atr:
        cands.append(px - 1.4 * atr)
    if r_now >= 1.0 and entry:
        cands.append(entry)  # breakeven after 1R
    if r_mfe >= 1.2 and mfe and entry:
        lock = entry + 0.45 * (mfe - entry)  # keep ~45% of MFE
        cands.append(lock)
    if r_now >= 1.6 and px and atr:
        cands.append(px - 1.0 * atr)  # tighter after TP1-area
    new_stop = old_stop
    valid = [s for s in cands if s and px and s < px * 0.998]
    if valid:
        new_stop = max(valid)
    if old_stop and new_stop and new_stop < old_stop:
        new_stop = old_stop
    if new_stop:
        pos["stop"] = px_round(new_stop)
    pos_plans[name] = {
        "entry": entry, "stop": pos.get("stop"), "tp1": pos.get("tp1"), "tp2": pos.get("tp2"),
        "atr4": atr, "r_now": pos.get("r_now"), "r_mfe": pos.get("r_mfe"), "why": why, "q": q,
    }
    # hours held
    hours = None
    try:
        if pos.get("opened"):
            ot = datetime.datetime.fromisoformat(pos["opened"])
            hours = (now_cest() - ot).total_seconds() / 3600.0
    except Exception:
        hours = None
    pos["hours_held"] = None if hours is None else round(hours, 2)
    action = None
    reason = None
    frac = "HELT"
    if px and old_stop and px <= old_stop:
        action, reason = "SÄLJ", "stop_hit"
    elif why in KILL_WHY:
        action, reason = "SÄLJ", f"struktur_dod {why} q{q}"
    elif px and pos.get("tp2") and px >= float(pos["tp2"]):
        action, reason = "SÄLJ", "tp2"
    elif px and pos.get("tp1") and px >= float(pos["tp1"]) and not pos.get("scaled_tp1"):
        action, reason, frac = "SÄLJ", "tp1_scale", "HÄLFTEN"
    elif r_mfe >= 1.2 and r_now <= 0.5 * r_mfe and r_now < 0.8:
        action, reason = "SÄLJ", f"giveback MFE{r_mfe:.2f}R nu{r_now:.2f}R"
    elif hours is not None and hours >= 10 and r_now < 0.35 and why not in ("RANGE_BREAK_HOLD", "HL_CONTINUATION"):
        action, reason = "SÄLJ", f"time_decay {hours:.1f}h r{r_now:.2f} {why}"
    elif hours is not None and hours >= 16 and r_now < 0.2:
        action, reason = "SÄLJ", f"time_stop {hours:.1f}h död drift"
    if action:
        exit_advice.append({
            "s": name, "op": action, "frac": frac, "px": px, "reason": reason,
            "why": why, "q": q, "r_now": pos.get("r_now"), "r_mfe": pos.get("r_mfe"),
        })
state["position_plans"] = pos_plans
state["exit_advice"] = exit_advice
# first advice is the command hint (one order per run)
state["exit_now"] = exit_advice[0] if exit_advice else None
# ROTATION v3.3.3: max 2 slots. Scanner decides NOTHING to buy.
# It only ranks open positions vs new A so the command engine can SÄLJ weakest then KÖP.
WEAK_WHY = {
    "FAILED_BREAK_TRAP", "EXT_BAN", "ISOLATED", "ISOLATED_PUMP", "HL_NO_PULLBACK",
    "1H_FAILED_HOLD", "15M_FAILED_HOLD", "BREAK_UNCONFIRMED",
}
held = [p.get("s") for p in (state.get("positions") or []) if p.get("s")]
new_a = [x for x in a_setups if x["s"] not in held]
def _pos_weak_score(pos):
    name = pos.get("s")
    row = next((x for x in struct if x["s"] == name), None)
    why = (row or {}).get("why") or ""
    q = fnum((row or {}).get("q")) or 0
    px = price_now.get(name)
    stop = fnum(pos.get("stop"))
    entry = fnum(pos.get("entry"))
    dist = 99.0
    if px and stop and px > 0:
        dist = abs(px - stop) / px * 100.0
    broken = 1 if why in WEAK_WHY else 0
    # higher = weaker
    return (broken, -q, -dist, why, name)
ranked_weak = sorted(state.get("positions") or [], key=_pos_weak_score, reverse=True)
weakest = ranked_weak[0] if ranked_weak else None
weak_row = next((x for x in struct if weakest and x["s"] == weakest.get("s")), None) if weakest else None
rotate = False
rotate_reason = None
if len(held) >= 2 and new_a and weakest:
    w_why = (weak_row or {}).get("why") or ""
    w_q = fnum((weak_row or {}).get("q")) or 0
    best_a = new_a[0]
    a_q = fnum(best_a.get("q")) or 0
    if w_why in WEAK_WHY and best_a.get("why") == "RANGE_BREAK_HOLD":
        rotate = True
        rotate_reason = f"svag {weakest.get('s')} {w_why} q{w_q} vs A {best_a['s']} q{a_q}"
    elif w_q <= 2 and a_q >= 5 and best_a.get("why") == "RANGE_BREAK_HOLD":
        rotate = True
        rotate_reason = f"låg q {weakest.get('s')} q{w_q} vs A {best_a['s']} q{a_q}"
state["rotation"] = {
    "max_pos": 2,
    "held": held,
    "slots_free": max(0, 2 - len(held)),
    "new_a": [x["s"] for x in new_a],
    "weakest": (weakest or {}).get("s") if weakest else None,
    "weakest_why": (weak_row or {}).get("why") if weak_row else None,
    "rotate": rotate,
    "reason": rotate_reason,
    "rule": "2 fulla + ny A tydligt bättre än svagaste (broken/failed/q<=2) -> SÄLJ svagaste HELT sen KÖP ny A. Byt inte vinnare som håller RANGE_BREAK_HOLD/HL. Aldrig 3 pos.",
}
state["rejected_now"] = {
    "ext_ban": out["ext_ban"],
    "failed_break": out["fb"][:8],
    "unconfirmed": out["unconfirmed"],
    "isolated": isolated[:8],
    "hl_no_pullback": out["hl_raw"][:8],
}
state["process_gaps_open"] = [
    g for g in [
        None if fng_val is not None else "fng SAKNAS",
        None if btc_dom is not None else "btc_dom SAKNAS",
        None if btc_funding is not None else "btc_funding SAKNAS",
        "trösklar låsta tills trade-n>=20 per why-klass",
        "nyheter/orderbok mellan timmar ej live",
    ] if g
]
dids = state.get("drive_file_ids") if isinstance(state.get("drive_file_ids"), dict) else {}
dids["scanner_v33_preferred"] = "1mizlCXNr8dQQQBHrDvXtiLkQPHMBE42Q"
dids["automation_task"] = "6c182af0-2afb-4b97-ae69-3aba47cafc8d"
dids["latest_complete_prev"] = dids.get("latest_upload") or dids.get("primary")
state["drive_file_ids"] = dids
state["display_lock"] = {
    "on": True,
    "size_lock": "alloc_sek/start_kassa låst vid fill",
    "mtm_vikt_not_buy": True,
    "fills_are_legs": True,
    "add_only_via_ADD": True,
    "engine": "v3.3.4-displaylock",
}
scan_action, held_now = reconcile_action(state)
fills_now = [f for f in (state.get("fills_applied") or []) if f.get("fill")]
if fills_now:
    scan_action = " ".join(f"{f['fill']} {f['s']}" for f in fills_now)
    state["last_action"] = scan_action
elif held_now:
    # no new fill this tick — log HÅLL, never replay old KÖP/SÄLJ
    if not (parse_command(scan_action) or {}).get("op") in ("HÅLL",):
        scan_action = "HÅLL " + " ".join(held_now)
        state["last_action"] = scan_action
else:
    scan_action = "CASH"
    state["last_action"] = "CASH"
hist = state.get("history") if isinstance(state.get("history"), list) else []
prev_act = (hist[-1].get("action") if hist else None)
hist_row = {
    "t": ts,
    "action": scan_action,
    "note": f"v333 4H{out['n_4h']}/80 1H{out['n_1h']} a={len(a_setups)} near={len(near_a)} regime={regime} flow={flow}",
    "a": [x["s"] for x in a_setups],
    "held": held_now,
    "mtm": state.get("total_value_sek"),
}
if prev_act != hist_row["action"] or not hist or (hist[-1].get("held") != held_now):
    hist.append(hist_row)
else:
    hist[-1] = hist_row
state["history"] = hist[-80:]
state["engine"] = "v3.3.4-displaylock"
state["version"] = "3.3.4"
state["scan_live_ts"] = ts
state["sanity"] = {"raw": sanity, "flag": flag, "median": med}
state["improving"] = out["improving"]
if "learn_lock" not in state:
    state["learn_lock"] = {"auto_tune": False}
atomic_json_write(STATE, state, ensure_ascii=False)
print("A", [x["s"] for x in a_setups])
print("NEAR", [x["s"] for x in near_a])
print("UNCONF", [x["s"] for x in unconf])
print("HL", [x["s"] for x in hl_ok])
print("IMPR", out["improving"])
print("REGIME", regime, "FLOW", flow_detail)
print("STATE_WRITTEN", STATE.stat().st_size)
print("DONE")
