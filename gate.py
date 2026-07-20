"""TOP-30 strong OI-spurt gate (live).

Returns (ranks: dict symbol->int rank by %OI rise desc, meta: dict status/source/count).
Strict rule: a symbol passes iff ranks[symbol] <= 30. If no feed is reachable, meta
status = 'OFFLINE' and NOTHING passes (strict mode, user choice).

Feed ladder for TODAY's spurt %s:
  1. NSE live OI-spurts API (tries several endpoint spellings, cookie-primed).
  2. Dhan futures intraday OI vs previous-day bhavcopy total (approximation, flagged).
"""
import os
import time
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}
TRY_URLS = [
    "https://www.nseindia.com/api/oi-spurts-underlyings",
    "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
    "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings?type=rise_in_oi",
]


def _rank_from_rows(items, allowed, pct_keys=("pChangeinOI", "pchange_in_oi", "perChangeInOI", "pct"), sym_keys=("symbol", "Symbol")):
    rows = []
    for it in items:
        sym, pct = None, None
        for k in sym_keys:
            if k in it:
                sym = str(it[k]).strip().upper()
        for k in pct_keys:
            if k in it:
                try:
                    pct = float(it[k])
                except (TypeError, ValueError):
                    pass
        if sym and pct is not None and sym in allowed:
            rows.append((sym, pct))
    rows.sort(key=lambda x: -x[1])
    return {sym: i + 1 for i, (sym, _) in enumerate(rows)}


def nse_live(universe):
    s = requests.Session()
    s.headers.update(UA)
    try:
        s.get("https://www.nseindia.com/api/allIndices", timeout=15)  # cookie prime
    except Exception:
        pass
    for u in TRY_URLS:
        for _ in range(2):
            try:
                r = s.get(u, headers={"Referer": "https://www.nseindia.com/market-data/oi-spurts"}, timeout=20)
                if not r.ok:
                    continue
                j = r.json()
                items = j.get("data") or j.get("underlying") or j
                if isinstance(items, list) and items:
                    ranks = _rank_from_rows(items, set(universe))
                    if ranks:
                        return ranks, {"status": "OK", "source": u, "count": len(ranks)}
            except Exception:
                time.sleep(2)
    return {}, {"status": "OFFLINE", "source": "nse-live", "count": 0}


def dhan_fut(universe, futmap, prev_oi, fetch_fut_fn, now_ist):
    """Approximate: near-month futures OI today vs prev-day total OI (fut+opt) from bhavcopy."""
    frm = now_ist.strftime("%Y-%m-%d 09:15:00")
    to = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    pcts = []
    for sym in universe:
        fid = futmap.get(sym)
        base = prev_oi.get(sym)
        if not fid or not base:
            continue
        try:
            df = fetch_fut_fn(fid, frm, to)
            if df is None or df.empty or pd.isna(df["oi"].iloc[-1]):
                continue
            pct = (float(df["oi"].iloc[-1]) - float(base)) / float(base) * 100
            pcts.append((sym, pct))
        except Exception:
            pass
        time.sleep(0.6)
    if not pcts:
        return {}, {"status": "OFFLINE", "source": "dhan-futures", "count": 0}
    pcts.sort(key=lambda x: -x[1])
    return {sym: i + 1 for i, (sym, _) in pcts}, \
           {"status": "OK-APPROX", "source": "dhan-futures(fut-only vs prev fut+opt)", "count": len(pcts)}


def evaluate(universe, now_ist, futmap=None, prev_oi=None, fetch_fut_fn=None):
    """-> (ranks, meta). Strict callers: pass only iff ranks[sym] <= 30 and meta OK."""
    ranks, meta = nse_live(universe)
    if ranks:
        return ranks, meta
    if futmap and prev_oi and fetch_fut_fn and os.environ.get("DHAN_TOKEN"):
        return dhan_fut(universe, futmap, prev_oi, fetch_fut_fn, now_ist)
    return {}, meta
