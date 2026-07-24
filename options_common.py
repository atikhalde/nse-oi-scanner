#!/usr/bin/env python3
"""Options-buying paper engine for M3/M4 (mirrors of live_runner/m2_runner).

Same master entries (identical B2 filter chain) — but instead of stock qty the
model BUys the nearest-expiry 1st-ITM option:
  BUY signal  -> 1st ITM Call (CE)    SELL signal -> 1st ITM Put (PE)

User-approved spec (23-Jul-2026, choices 1B 2A 3A 4A 5A):
  * Entry fill = option LTP at signal-bar close (paper).
  * SL computed ONCE at entry in premium terms (1B): underlying structure SL =
    signal-bar low/high ∓0.02% buffer; premium risk R = |delta| x UL distance;
    prem_sl = entry_prem - R. Hard cap: skip trade if ONE lot's risk > Rs1,900
    (Rs2,000 max-loss cap - Rs100 buffer, mirrors the stock Rs900/Rs1,000 rule).
  * TP1 = 1:1 on premium -> book 50% qty there.
  * Remaining 50% trails on UNDERLYING structure swings (3A) — swing pivots on
    the stock's 5-min bars (same pivot shape as the stock engine); when the
    underlying crosses the trail stop, the option exits at that bar's close.
  * Lots: max lots s.t. risk <= cap AND premium outlay <= Rs50,000 (4A).
  * 15:20 IST square-off. Bar-close/option-bar fills, documented paper rules.

Dhan endpoints used (access-token header; client-id sent when present):
  POST /v2/optionchain/expirylist  {UnderlyingScrip, UnderlyingSeg:"NSE_EQ"}
  POST /v2/optionchain             {... + Expiry:"YYYY-MM-DD"}   (1 req / 3s!)
  POST /v2/marketfeed/ltp          {"NSE_FNO":[sec_id,...]}
  POST /v2/charts/intraday         OPTSTK 5-min candles for open positions
Lot sizes: Dhan public scrip-master CSV (no token), cached to data/opt_lots.json.

If any option feed is unreachable, marks fall back to a Black-Scholes synthetic
(frozen IV/delta from entry) — every such fill is labeled src="synth" so it can
never masquerade as a real quote.
"""
import csv
import io
import json
import math
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
LOTS_CACHE = ROOT / "data" / "opt_lots.json"
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

BUFFER = 0.0002                 # 0.02% structure buffer (same as stock engine)
SQOFF = "15:20"
OPT_RISK_CAP = 1900.0           # planned max loss (Rs2,000 cap - Rs100 buffer)
OPT_CAPITAL = 50000.0           # premium-outlay cap per trade (4A)
DELTA_FALLBACK = 0.60           # 1st-ITM typical
R_FREE = 0.07
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}

OPT_RULES_NOTE = ("OPTIONS BUYING · nearest expiry · 1st ITM · prem=entry LTP · SL prem = entry "
                  "− delta×UL-risk (UL signal-bar low/high ∓0.02%) · max 1-lot risk ₹2,000 (skip if 1 lot "
                  "breaches) · outlay ≤ ₹50k · TP 1:1 book 50% · rest 50% trails UL structure swings · "
                  "sq-off 15:20 · costs INCLUDED (₹20/order + STT 0.15% sell-prem + txn 0.03553% + GST)")

_last_chain_ts = [0.0]
_chain_cache = {}

# ------------------------------------------------------------------ http/dhan
def _hdr():
    h = {"Content-Type": "application/json", "access-token": os.environ.get("DHAN_TOKEN", "")}
    if os.environ.get("DHAN_CLIENT_ID"):
        h["client-id"] = os.environ["DHAN_CLIENT_ID"]
    return h


def _post(url, payload, timeout=25):
    r = requests.post(url, json=payload, headers=_hdr(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def expiry_list(symbol, sid):
    """Nearest expiry (>= today, exchange truth) for a stock underlying."""
    j = _post("https://api.dhan.co/v2/optionchain/expirylist",
              {"UnderlyingScrip": int(sid), "UnderlyingSeg": "NSE_EQ"})
    dates = [d for d in (j.get("data") or []) if isinstance(d, str)]
    dates.sort()
    import datetime as dt
    today = dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")
    for d in dates:
        if d >= today:
            return d
    return dates[-1] if dates else None


def fetch_chain(symbol, sid, expiry):
    """Full option chain for the expiry (rate limit 1 req / 3s — spaced+cached)."""
    key = (symbol, expiry)
    if key in _chain_cache and time.time() - _chain_cache[key][0] < 45:
        return _chain_cache[key][1]
    wait = 3.2 - (time.time() - _last_chain_ts[0])
    if wait > 0:
        time.sleep(wait)
    j = _post("https://api.dhan.co/v2/optionchain",
              {"UnderlyingScrip": int(sid), "UnderlyingSeg": "NSE_EQ", "Expiry": expiry}, timeout=30)
    _last_chain_ts[0] = time.time()
    data = j.get("data") or {}
    _chain_cache[key] = (time.time(), data)
    return data


def _num(x, d=None):
    try:
        v = float(x)
        return v if math.isfinite(v) else d
    except Exception:
        return d


def pick_itm1(chain, spot, side):
    """1st ITM strike: BUY->CE (highest strike < spot), SELL->PE (lowest > spot).
    Returns dict(sec_id, strike, opt_type, ltp, delta, sigma, bid, ask)."""
    oc = chain.get("oc") or {}
    strikes = sorted(_num(k) for k in oc.keys() if _num(k) is not None)
    if not strikes:
        return {"error": "empty option chain"}
    if side == "BUY":
        cands = [k for k in strikes if k < spot]
        if not cands:
            return {"error": "no ITM call strike below spot"}
        strike = max(cands)
        leg = (oc.get(f"{strike:.6f}") or {}).get("ce") or {}
        opt = "CE"
    else:
        cands = [k for k in strikes if k > spot]
        if not cands:
            return {"error": "no ITM put strike above spot"}
        strike = min(cands)
        leg = (oc.get(f"{strike:.6f}") or {}).get("pe") or {}
        opt = "PE"
    ltp = _num(leg.get("last_price"), 0) or 0
    bid = _num(leg.get("top_bid_price")); ask = _num(leg.get("top_ask_price"))
    if (not ltp or ltp <= 0) and bid and ask:
        ltp = round((bid + ask) / 2, 2)
    delta = _num((leg.get("greeks") or {}).get("delta"))
    iv = _num(leg.get("implied_volatility"))
    sigma = iv / 100.0 if iv and iv > 1.5 else iv           # dhan sends % (e.g. 9.8)
    return {"sec_id": int(leg.get("security_id") or 0), "strike": strike, "opt_type": opt,
            "ltp": ltp, "delta": abs(delta) if delta else None,
            "sigma": sigma if sigma and 0.02 < sigma < 3 else None,
            "bid": bid, "ask": ask}


def ltp_many(sec_ids):
    """Live LTPs for option security ids -> {sec_id:int -> price:float}"""
    out = {}
    ids = [int(s) for s in sec_ids if s]
    if not ids:
        return out
    for i in range(0, len(ids), 900):                       # api caps list size
        j = _post("https://api.dhan.co/v2/marketfeed/ltp", {"NSE_FNO": ids[i:i + 900]})
        for k, v in ((j.get("data") or {}).get("NSE_FNO") or {}).items():
            p = _num((v or {}).get("last_price"))
            if p:
                out[int(k)] = p
    return out


# ------------------------------------------------------------------ lot size
def _lots_cache_load():
    try:
        return json.loads(LOTS_CACHE.read_text())
    except Exception:
        return {}


def _lots_cache_save(c):
    LOTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LOTS_CACHE.write_text(json.dumps(c, indent=1))


def lot_size(sec_id):
    """Exchange lot size for an option security id (scrip-master, cached)."""
    c = _lots_cache_load()
    k = str(int(sec_id))
    if k in c:
        return int(c[k])
    r = requests.get(SCRIP_URL, headers=UA, stream=True, timeout=120)
    r.raise_for_status()
    buf, lot = [], None
    for chunk in r.iter_content(1 << 20):
        if isinstance(chunk, (bytes, bytearray)):        # requests 3.x ignores
            chunk = chunk.decode("utf-8", "ignore")      # decode_unicode here — decode by hand
        buf.append(chunk)
        txt = "".join(buf)
        lines = txt.split("\n")
        buf = [lines.pop()]                                  # keep partial line
        for row in csv.reader(lines):
            if len(row) >= 7 and row[2] == k and row[3] == "OPTSTK":
                lot = int(float(row[6]))
                break
        if lot:
            break
    if not lot:
        raise ValueError(f"lot size not found for sec_id {k}")
    c[k] = lot
    _lots_cache_save(c)
    return lot


# ------------------------------------------------------------------ black-scholes (synth fallback)
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S, K, T, sigma, call=True, r=R_FREE):
    if T <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    vol = sigma * math.sqrt(T)
    if vol <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / vol
    d2 = d1 - vol
    return (S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)) if call else \
           (K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1))


def bs_delta(S, K, T, sigma, call=True, r=R_FREE):
    if T <= 0:
        return 1.0 if (call and S > K) else ( -1.0 if ((not call) and S < K) else 0.0)
    vol = sigma * math.sqrt(T)
    if vol <= 0:
        return 1.0 if call else -1.0
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / vol
    return _ncdf(d1) if call else _ncdf(d1) - 1


def synth_prem(S, tr):
    """BS mark for the stored contract (frozen sigma from entry)."""
    import datetime as dt
    T = max(0.0, (dt.date.fromisoformat(tr["expiry"]) - dt.date.fromisoformat(tr["today"])).days) / 365
    return bs_price(S, tr["strike"], T, tr.get("sigma") or 0.35, tr["opt_type"] == "CE")


# ------------------------------------------------------------------ option bars (real quotes)
def today_opt_bars(tr, now_ist):
    """5-min option candles today via Dhan charts (OPTSTK). None on failure."""
    sid = tr.get("sec_id")
    if not sid or not os.environ.get("DHAN_TOKEN"):
        return None
    today = now_ist.strftime("%Y-%m-%d")
    frm = f"{today} 09:15:00"
    to = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    j = _post("https://api.dhan.co/v2/charts/intraday",
              {"securityId": str(sid), "exchangeSegment": "NSE_FNO", "instrument": "OPTSTK",
               "interval": "5", "oi": False, "fromDate": frm, "toDate": to}, timeout=30)
    if not j.get("timestamp"):
        return None
    import pandas as pd
    df = pd.DataFrame({"dt": pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("Asia/Kolkata"),
                       "open": j["open"], "high": j["high"], "low": j["low"],
                       "close": j["close"], "volume": j.get("volume")}).dropna()
    if df.empty:
        return None
    df = df.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    df["t"] = df["dt"].dt.strftime("%H:%M")
    return df


# ------------------------------------------------------------------ entry
def enter(sym, side, etime, ul_entry, signal, ul_bars, now_ist, sid=None, hist_csv=None):
    """Build an options paper trade for a master signal. Mirrors trader.evaluate's
    return shape so report.py renders it unchanged."""
    import pandas as pd
    ei_l = ul_bars.index[ul_bars["t"] == etime].tolist()
    if not ei_l:
        return {"symbol": sym, "error": "entry bar missing"}
    ei = ei_l[0]
    bar_lo = float(ul_bars["low"].iloc[ei]); bar_hi = float(ul_bars["high"].iloc[ei])
    ul_sl = bar_lo * (1 - BUFFER) if side == "BUY" else bar_hi * (1 + BUFFER)
    ul_risk = abs(ul_entry - ul_sl)
    if ul_risk <= 0:
        return {"symbol": sym, "error": "degenerate UL SL"}

    # --- contract: nearest expiry, 1st ITM
    prem_src, contract, sigma, delta, expiry = "live", None, None, None, None
    try:
        expiry = expiry_list(sym, sid)
        chain = fetch_chain(sym, sid, expiry) if expiry else None
        pk = pick_itm1(chain, ul_entry, side) if chain else {"error": "no chain"}
        if "error" not in pk and pk.get("ltp", 0) > 0:
            contract, sigma, delta = pk, pk.get("sigma"), pk.get("delta")
        else:
            contract = {**(pk if "error" not in pk else {})}
            contract["err_msg"] = pk.get("error")
    except Exception as e:
        contract = {"err_msg": f"{type(e).__name__}: {e}"}

    if contract is None or not contract.get("strike"):
        # synth contract: strike = nearest-round 1st ITM guess (flagged)
        return {"symbol": sym, "error": f"option chain unavailable ({contract.get('err_msg') if contract else 'n/a'}) — skipped (strict: real quotes needed at entry)"}

    strike = contract["strike"]; opt = contract["opt_type"]
    premium = _num(contract.get("ltp"), 0) or 0
    if premium <= 0:
        prem_src = "synth"
        today = now_ist.strftime("%Y-%m-%d")
        tr_tmp = {"expiry": expiry, "today": today, "strike": strike, "opt_type": opt,
                  "sigma": sigma or _hv_sigma(hist_csv)}
        premium = round(synth_prem(ul_entry, tr_tmp), 2)
    if sigma is None:
        sigma = _hv_sigma(hist_csv)
    if delta is None:
        import datetime as dt
        T = max(0.0, (dt.date.fromisoformat(expiry) - dt.date.fromisoformat(now_ist.strftime("%Y-%m-%d"))).days) / 365
        delta = abs(bs_delta(ul_entry, strike, T, sigma, opt == "CE")) or DELTA_FALLBACK

    r_prem = delta * ul_risk                       # premium risk per unit (1B)
    if r_prem <= 0 or r_prem >= premium:
        return {"symbol": sym, "error": f"option R invalid (R={r_prem:.2f} prem={premium:.2f})"}
    prem_sl = premium - r_prem
    tp1 = premium + r_prem                         # 1:1 on premium

    try:
        lot = lot_size(contract["sec_id"]) if contract.get("sec_id") else None
    except Exception as e:
        return {"symbol": sym, "error": f"lot size: {e}"}
    if not lot:
        return {"symbol": sym, "error": "lot size unknown (no sec_id)"}
    risk_lot = r_prem * lot
    outlay_lot = premium * lot
    lots = int(min(OPT_RISK_CAP // risk_lot, OPT_CAPITAL // outlay_lot))
    if lots < 1:
        why = "₹2,000 max-loss" if OPT_RISK_CAP // risk_lot < 1 else "₹50k premium-outlay cap"
        return {"symbol": sym, "error": f"1 lot breaches {why} (risk/lot ₹{risk_lot:.0f}, outlay/lot ₹{outlay_lot:.0f}) — skipped"}
    qty = lots * lot
    risk_rs = risk_lot * lots
    contract_name = f"{sym} {strike:g}{opt} {dt_str_short(expiry)}"
    return {
        "symbol": sym, "side": "BUY", "orig_side": side, "time": etime, "signal": f"{signal} → {opt}",
        "setup": "OPTIONS-BUY", "entry": round(premium, 2), "sl": round(prem_sl, 2),
        "sl_anchor": f"UL signal-bar {'low' if side=='BUY' else 'high'} ∓0.02% ×Δ{delta:.2f}",
        "risk_pts": round(r_prem, 2), "risk_pct": round(r_prem / premium * 100, 2),
        "risk_rs": round(risk_rs, 0), "qty": qty, "qty_full": qty, "qty_capped": lots < int(OPT_CAPITAL // outlay_lot),
        "capital": round(premium * qty, 0), "tp1": round(tp1, 2), "tp2": None,
        "contract": contract_name, "expiry": expiry, "strike": strike, "opt_type": opt,
        "sec_id": contract.get("sec_id"), "lot_size": lot, "lots": lots,
        "ul_entry": round(ul_entry, 2), "ul_sl": round(ul_sl, 2), "delta": round(delta, 3),
        "sigma": round(sigma, 4), "prem_src": prem_src, "today": now_ist.strftime("%Y-%m-%d"),
        "trail_armed": False, "trail_style": "underlying structure swings",
    }


def dt_str_short(iso):
    try:
        import datetime as dt
        return dt.date.fromisoformat(iso).strftime("%d%b").upper()
    except Exception:
        return str(iso)


def _hv_sigma(hist_csv):
    """20-day historical vol (annualized) from repo history; fallback 0.35."""
    try:
        import pandas as pd
        h = pd.read_csv(hist_csv, parse_dates=["dt"])
        d = h.groupby(h["dt"].dt.date)["close"].last().dropna().tail(21)
        if len(d) < 6:
            return 0.35
        lr = (d / d.shift()).apply(math.log).dropna()
        return max(0.10, min(1.5, float(lr.std() * math.sqrt(252))))
    except Exception:
        return 0.35


# ------------------------------------------------------------------ exits (deterministic re-eval)
def evaluate_opt(tr, ul_bars, opt_bars=None):
    """Walk today bars from entry; fills per paper rules. tr: stored dict from
    enter(); ul_bars: today's underlying df (+t); opt_bars: today's option df or
    None -> BS synth marks from ul bars (labeled). Mirrors trader.evaluate shape."""
    import pandas as pd
    ei_l = ul_bars.index[ul_bars["t"] == tr["time"]].tolist()
    if not ei_l:
        return {**tr, "error": "entry bar missing"}
    ei = ei_l[0]
    entry, prem_sl, tp1 = float(tr["entry"]), float(tr["sl"]), float(tr["tp1"])
    qty, q1 = int(tr["qty"]), int(int(tr["qty"]) * 0.5)
    ul_dir = 1 if tr["opt_type"] == "CE" else -1
    is_ce = tr["opt_type"] == "CE"
    stop_ul = float(tr["ul_sl"])
    src = tr.get("prem_src", "live")

    omap = {}
    if opt_bars is not None and len(opt_bars):
        for _, r in opt_bars.iterrows():
            omap[str(r["t"])] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    else:
        src = "synth"

    def mark(t, uo, uh, ul, uc):
        if t in omap:
            return omap[t]
        # synth from this bar's ul prices (frozen IV/delta)
        po = synth_prem(uo, tr); pc = synth_prem(uc, tr)
        ph = synth_prem(uh if is_ce else ul, tr); pl = synth_prem(ul if is_ce else uh, tr)
        return (po, ph, pl, pc)

    got1 = False
    legs, events = [], [{"key": "ENTRY", "time": tr["time"], "price": entry}]
    closed, open_q = False, qty
    exit_t = None
    ulo, uhi, ulo_, ucl = ul_bars["open"].values, ul_bars["high"].values, ul_bars["low"].values, ul_bars["close"].values
    for j in range(ei + 1, len(ul_bars)):
        uo, uh, ul, uc = float(ulo[j]), float(uhi[j]), float(ulo_[j]), float(ucl[j])
        t = str(ul_bars["t"].iloc[j])
        po, ph, pl, pc = mark(t, uo, uh, ul, uc)
        # 1) premium SL first (gap-through fills at option open)
        if pl <= prem_sl:
            px = po if po < prem_sl else prem_sl
            lbl = "trail" if got1 else "SL"
            legs.append((f"{lbl} {t}", open_q, px, t))
            events.append({"key": "EXIT_SL", "time": t, "price": px})
            closed, exit_t = True, t
            break
        # 2) TP1 1:1 book 50%
        if not got1 and ph >= tp1:
            got1 = True
            legs.append((f"T1 {t}", q1, tp1, t)); open_q -= q1
            events.append({"key": "TP1", "time": t, "price": tp1})
        # 3) underlying-structure trail (arms at TP1; stop hands over next bar)
        if got1:
            if ul_dir == 1 and ul <= stop_ul:                     # trail touched
                legs.append((f"trail {t}", open_q, pc, t))
                events.append({"key": "EXIT_SL", "time": t, "price": pc})
                closed, exit_t = True, t
                break
            if ul_dir == -1 and uh >= stop_ul:
                legs.append((f"trail {t}", open_q, pc, t))
                events.append({"key": "EXIT_SL", "time": t, "price": pc})
                closed, exit_t = True, t
                break
            k = j - 2                                              # pivot candidate (confirmed 2 bars later)
            if k - 2 >= 0 and k + 2 < len(ul_bars):
                if ul_dir == 1 and ulo_[k] < ulo_[k - 1] and ulo_[k] < ulo_[k - 2] and ulo_[k] < ulo_[k + 1] and ulo_[k] < ulo_[k + 2]:
                    stop_ul = max(stop_ul, float(ulo_[k]) * (1 - BUFFER))
                if ul_dir == -1 and uhi[k] > uhi[k - 1] and uhi[k] > uhi[k - 2] and uhi[k] > uhi[k + 1] and uhi[k] > uhi[k + 2]:
                    stop_ul = min(stop_ul, float(uhi[k]) * (1 + BUFFER))
        # 4) square-off
        if t == SQOFF:
            legs.append((f"EOD {t}", open_q, pc, t))
            events.append({"key": "EXIT_EOD", "time": t, "price": pc})
            closed, exit_t = True, t
            break
    if not closed:
        last_t = str(ul_bars["t"].iloc[-1])
        pc = mark(last_t, float(ulo[-1]), float(uhi[-1]), float(ulo_[-1]), float(ucl[-1]))[3]
        legs.append((f"OPEN {last_t}", open_q, pc, last_t))

    pnl = sum((px - entry) * q for _l, q, px, _t in legs)          # long premium
    risk_rs = (entry - prem_sl) * qty
    parts = []
    for lbl, q, px, t in legs:
        if lbl.startswith("T1"):
            parts.append(f"50% TP@1:1 {t}")
        elif lbl.startswith("OPEN"):
            parts.append("OPEN")
        else:
            pct = round(100 * q / qty)
            kind = "trail" if lbl.startswith("trail") else ("SL" if lbl.startswith("SL") else "EOD")
            parts.append(f"{pct}% {kind} {t}")
    out = dict(tr)
    out.update({"legs": legs, "events": events, "exit_text": " · ".join(parts),
                "leg2_time": legs[-1][3] if legs else None,
                "pnl": round(pnl, 0), "r_total": round(pnl / risk_rs, 2) if risk_rs else 0.0,
                "closed": closed, "trail_ul_stop": round(stop_ul, 2), "prem_src": src})
    return out


# ------------------------------------------------------------------ alerts
def fmt_opt_alert(tr, key, tag="🅼3"):
    c = tr.get("contract", tr["symbol"])
    if key == "ENTRY":
        src = "live LTP" if tr.get("prem_src") == "live" else "⚠️ synth px"
        return (f"{tag} 🟢 <b>OPTION BUY — {c}</b>\n"
                f"{tr['signal']} @ {tr['time']} · UL ₹{tr['ul_entry']:,.2f} → prem ₹{tr['entry']:,.2f} ({src})\n"
                f"{tr['lots']} lot × {tr['lot_size']} = {tr['qty']} · outlay ₹{tr['capital']:,.0f}\n"
                f"SL ₹{tr['sl']:,.2f} (UL {tr['ul_sl']:,.2f}) · max-loss ₹{tr['risk_rs']:,.0f} · TP1 ₹{tr['tp1']:,.2f} (1:1, book 50%)")
    if key == "TP1":
        return (f"{tag} 💰 <b>TP1 1:1 — {c}</b>\n"
                f"50% booked @ ₹{tr['tp1']:,.2f} · rest 50% trails UL structure swings")
    if key in ("EXIT_SL", "EXIT_EOD"):
        last = tr["legs"][-1] if tr.get("legs") else ("", 0, 0, "")
        emoji = "🛑" if tr.get("pnl", 0) < 0 else "🏁"
        return (f"{tag} {emoji} <b>EXIT — {c}</b>\n"
                f"{tr.get('exit_text', '')}\nP&L ₹{tr.get('pnl', 0):,.0f} ({tr.get('r_total', 0):+.2f}R)")
    return f"{tag} {key} — {c}"
