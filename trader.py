"""Paper-trade state machine v2 — SETUP-AWARE EXITS (user spec, 22-Jul-2026).

Entry : master signal (untouched engine), fill at signal-bar close.

Setup classification (at entry bar, TradingView-style price action):
  BREAKOUT      entry within 0.30xATR of the 20-bar/day extreme (riding highs/lows)
  PULLBACK      trend intact (close above/below EMA20), dip-touched EMA20 zone in last ~6 bars
  CONTINUATION  deep in trend, riding EMA9/EMA20, not at a fresh extreme
  REVERSAL      entry against the day's move, near the OPPOSITE day extreme

Initial SL (structure per setup — NEVER tightened artificially):
  BREAKOUT / CONTINUATION : last confirmed 5-min pivot low (BUY) / pivot high (SELL) ∓0.02%
  PULLBACK                : pullback low/high (last ~6 bars incl. entry) ∓0.02%  [tight, low-risk]
  REVERSAL                : day extreme ∓0.02%; if extreme is >2xATR away, 1.5xATR from entry
  fallback when no structure: session extreme before entry.

MAX-LOSS CAP (user): planned loss <= Rs900 (= Rs1,000 worst-case incl. Rs100 gap buffer).
  qty = floor(50,000 / entry); if qty * risk_pts > 900 -> qty = floor(900 / risk_pts)  (min 1).
  The SL itself always stays at true structure.

Targets (partial booking): 50% @ 1:1.5 · 30% @ 1:2.5 · final 20% trailed.
  Breakeven floor: once TP1 banks, SL is floored at entry (never a red trade again).
Trail (arms once MFE >= +1R, re-evaluated after every bar close, applies next bar):
  BREAKOUT / PULLBACK : confirmed 5-min swing structure (pivot ∓0.02%)
  CONTINUATION        : EMA9 trail (+structure floor)
  REVERSAL            : 2xATR chandelier from best price
Fill rules: SL checked FIRST inside a bar (gap-through fills at open); TP fills exact;
square-off at the 15:20 bar close.

evaluate() is DETERMINISTIC over (today-bars + static warmup tail) -> safe to re-run
every live cycle; alerting dedups via event keys.
"""
import pandas as pd

CAPITAL = 50000
BUFFER = 0.0002
SQOFF = "15:20"
RISK_CAP = 900          # planned max loss (Rs1,000 cap - Rs100 gap buffer) — user spec
TP1_R, TP1_FRAC = 1.5, 0.50
TP2_R, TP2_FRAC = 2.5, 0.30
TRAIL_ARM_R = 1.0
NEAR_ATR = 0.30
PULLBACK_BARS = 5       # look-back (in addition to entry bar) for pullback zone/low
REV_SL_ATR = 1.5        # reversal entry fallback / extreme-too-far hard SL
REV_EXT_ATR = 2.0       # beyond this, day extreme is "too far" -> use REV_SL_ATR
REV_TRAIL_ATR = 2.0     # reversal chandelier distance
WARMUP_BARS = 400       # history tail for EMA/ATR seeding (matches analysis frame)

SETUP_LABEL = {
    "BREAKOUT": "Breakout",
    "PULLBACK": "Pullback (low-risk)",
    "CONTINUATION": "Trend continuation",
    "REVERSAL": "Reversal",
}
TRAIL_LABEL = {
    "BREAKOUT": "structure swings",
    "PULLBACK": "structure swings",
    "CONTINUATION": "EMA9",
    "REVERSAL": "2xATR chandelier",
}


def load_warmup(hist_csv, today, n=WARMUP_BARS):
    """History tail bars EXCLUDING `today` (YYYY-MM-DD) for EMA/ATR seeding."""
    from pathlib import Path
    fp = Path(hist_csv)
    if not fp.exists():
        return None
    try:
        h = pd.read_csv(fp, parse_dates=["dt"])
        if h.empty:
            return None
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata") \
            if h["dt"].dt.tz is None else h["dt"]
        h = h[h["dt"].dt.strftime("%Y-%m-%d") != today].sort_values("dt").tail(n)
        return h[["open", "high", "low", "close"]].reset_index(drop=True)
    except Exception:
        return None


def _indicators(warm, bars):
    """EMA9/EMA20/ATR14(RMA) computed on warmup+today, returned aligned to `bars`."""
    base = pd.concat([warm, bars[["open", "high", "low", "close"]].reset_index(drop=True)],
                     ignore_index=True) if warm is not None and len(warm) else \
        bars[["open", "high", "low", "close"]].reset_index(drop=True)
    e9 = base["close"].ewm(span=9, adjust=False).mean().values
    e20 = base["close"].ewm(span=20, adjust=False).mean().values
    tr = pd.concat([base["high"] - base["low"],
                    (base["high"] - base["close"].shift()).abs(),
                    (base["low"] - base["close"].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().values
    off = len(base) - len(bars)
    return e9, e20, atr, off


def classify_setup(bars, i, side, atr_i, e20_i):
    """Price-action setup at entry bar i (exact port of the verified analysis rules)."""
    c = float(bars["close"].iloc[i])
    near = NEAR_ATR * atr_i
    hi20 = bars["high"].iloc[max(0, i - 20):i].max() if i > 0 else -1e18
    lo20 = bars["low"].iloc[max(0, i - 20):i].min() if i > 0 else 1e18
    day_hi = bars["high"].iloc[:i].max() if i > 0 else -1e18
    day_lo = bars["low"].iloc[:i].min() if i > 0 else 1e18
    seg = bars.iloc[max(0, i - PULLBACK_BARS):i + 1]
    if side == "BUY":
        if c >= hi20 - near and i > 0:
            return "BREAKOUT"
        if c <= day_lo + near and c < e20_i and i > 0:
            return "REVERSAL"
        if c > e20_i and seg["low"].min() <= e20_i + near:
            return "PULLBACK"
        return "CONTINUATION"
    if c <= lo20 + near and i > 0:
        return "BREAKOUT"                    # breakdown short
    if c >= day_hi - near and c > e20_i and i > 0:
        return "REVERSAL"
    if c < e20_i and seg["high"].max() >= e20_i - near:
        return "PULLBACK"
    return "CONTINUATION"


def _pivot_sl(bars, i, side):
    """Last confirmed 5-min swing before entry (trader v1 behaviour, kept as structure)."""
    lows, highs = bars["low"].values, bars["high"].values
    piv = None
    for k in range(2, i - 2):
        if side == "BUY" and lows[k] < lows[k - 1] and lows[k] < lows[k - 2] and lows[k] < lows[k + 1] and lows[k] < lows[k + 2]:
            piv = lows[k]
        if side == "SELL" and highs[k] > highs[k - 1] and highs[k] > highs[k - 2] and highs[k] > highs[k + 1] and highs[k] > highs[k + 2]:
            piv = highs[k]
    if piv is None:
        raw = lows[:i].min() if side == "BUY" else highs[:i].max()
        return (raw * (1 - BUFFER) if side == "BUY" else raw * (1 + BUFFER)), "session-extreme*"
    return (piv * (1 - BUFFER) if side == "BUY" else piv * (1 + BUFFER)), "pivot"


def setup_sl(bars, i, side, cls, atr_i):
    psl, anchor = _pivot_sl(bars, i, side)
    entry = float(bars["close"].iloc[i])      # only used for degenerate fallbacks
    if cls == "PULLBACK":
        seg = bars.iloc[max(0, i - PULLBACK_BARS):i + 1]
        raw = seg["low"].min() if side == "BUY" else seg["high"].max()
        sl = raw * (1 - BUFFER) if side == "BUY" else raw * (1 + BUFFER)
        anchor = "pullback-high/low"
        if side == "BUY":
            sl = min(sl, entry - 0.05 * atr_i)          # never absurdly tight
            sl = max(sl, entry - REV_SL_ATR * atr_i)    # sanity floor
        else:
            sl = max(sl, entry + 0.05 * atr_i)
            sl = min(sl, entry + REV_SL_ATR * atr_i)
    elif cls == "REVERSAL":
        ext = bars["low"].iloc[:i].min() * (1 - BUFFER) if side == "BUY" \
            else bars["high"].iloc[:i].max() * (1 + BUFFER)
        anchor = "day-extreme"
        if side == "BUY":
            sl = ext if entry - ext <= REV_EXT_ATR * atr_i else entry - REV_SL_ATR * atr_i
        else:
            sl = ext if ext - entry <= REV_EXT_ATR * atr_i else entry + REV_SL_ATR * atr_i
    else:
        sl = psl
    # degenerate guard: SL must sit beyond entry on the risk side
    if (side == "BUY" and sl >= entry) or (side == "SELL" and sl <= entry):
        sl = entry - atr_i if side == "BUY" else entry + atr_i
        anchor = anchor + "+ATR-guard"
    return sl, anchor


def evaluate(sym, side, etime, entry, signal, bars, warmup=None, today_date=None):
    """bars: today's 5-min df with 't' column (+ optional warmup tail for indicators).
    Returns trade dict with events[] and legs[] (deterministic over the bar set)."""
    ei_list = bars.index[bars["t"] == etime].tolist()
    if not ei_list:
        return {"symbol": sym, "error": "entry bar missing"}
    ei = ei_list[0]
    e9, e20, atr, off = _indicators(warmup, bars)
    atr_i = float(atr[off + ei]) if pd.notna(atr[off + ei]) else \
        float((bars["high"] - bars["low"]).iloc[:ei + 1].mean())
    e20_i = float(e20[off + ei])
    cls = classify_setup(bars, ei, side, atr_i, e20_i)
    sl, anchor = setup_sl(bars, ei, side, cls, atr_i)
    s = 1 if side == "BUY" else -1
    risk = abs(entry - sl)
    if risk <= 0:
        return {"symbol": sym, "error": "SL beyond entry"}
    risk_pct = risk / entry * 100
    qty = int(CAPITAL // entry)
    qty_full = qty
    if qty * risk > RISK_CAP:                       # max-loss cap: shrink qty, SL untouched
        qty = max(1, int(RISK_CAP // risk))
    if qty < 1:
        return {"symbol": sym, "error": "qty=0"}
    q1, q2 = int(qty * TP1_FRAC), int(qty * TP2_FRAC)
    q3 = qty - q1 - q2
    tp1, tp2 = entry + s * TP1_R * risk, entry + s * TP2_R * risk
    stop, best, mfe_r = sl, entry, 0.0
    got1 = got2 = armed = False
    legs, events = [], [{"key": "ENTRY", "time": etime, "price": entry}]
    closed, exit_t = False, None
    open_q = qty

    for j in range(ei + 1, len(bars)):
        o = float(bars["open"].iloc[j]); h = float(bars["high"].iloc[j])
        l = float(bars["low"].iloc[j]); c = float(bars["close"].iloc[j]); t = str(bars["t"].iloc[j])
        # 1) SL first (gap-through fills at open)
        if (s == 1 and l <= stop) or (s == -1 and h >= stop):
            px = o if (s == 1 and o < stop) or (s == -1 and o > stop) else stop
            lbl = "trail" if armed else "SL"
            legs.append((f"{lbl} {t}", open_q, px, t))
            events.append({"key": "EXIT_SL", "time": t, "price": px})
            closed, exit_t = True, t
            break
        # 2) partial books
        if not got1 and ((s == 1 and h >= tp1) or (s == -1 and l <= tp1)):
            got1 = True
            legs.append((f"T1 {t}", q1, tp1, t)); open_q -= q1
            events.append({"key": "TP1", "time": t, "price": tp1})
        if got1 and not got2 and q2 > 0 and ((s == 1 and h >= tp2) or (s == -1 and l <= tp2)):
            got2 = True
            legs.append((f"T2 {t}", q2, tp2, t)); open_q -= q2
            events.append({"key": "TP2", "time": t, "price": tp2})
        # 3) trail updates AFTER bar close -> apply from next bar
        best = max(best, h) if s == 1 else min(best, l)
        mfe_r = max(mfe_r, s * (best - entry) / risk)
        if mfe_r >= TRAIL_ARM_R:
            if not armed:
                armed = True
                events.append({"key": "TRAIL_ON", "time": t, "price": h if s == 1 else l})
            cand = stop
            atr_j = float(atr[off + j]) if pd.notna(atr[off + j]) else atr_i
            if cls in ("BREAKOUT", "PULLBACK"):
                k = j - 2
                if k - 2 >= 0 and k + 2 < len(bars):
                    lo, hi = bars["low"].values, bars["high"].values
                    if s == 1 and lo[k] < lo[k - 1] and lo[k] < lo[k - 2] and lo[k] < lo[k + 1] and lo[k] < lo[k + 2]:
                        cand = max(cand, float(lo[k]) * (1 - BUFFER))
                    if s == -1 and hi[k] > hi[k - 1] and hi[k] > hi[k - 2] and hi[k] > hi[k + 1] and hi[k] > hi[k + 2]:
                        cand = min(cand, float(hi[k]) * (1 + BUFFER))
            elif cls == "CONTINUATION":
                e = float(e9[off + j])
                cand = max(cand, e * (1 - BUFFER)) if s == 1 else min(cand, e * (1 + BUFFER))
            else:  # REVERSAL chandelier
                cand = max(cand, best - REV_TRAIL_ATR * atr_j) if s == 1 \
                    else min(cand, best + REV_TRAIL_ATR * atr_j)
            stop = cand
        if got1:                          # breakeven floor once TP1 banked (applies next bar)
            stop = max(stop, entry) if s == 1 else min(stop, entry)
        # 4) square-off
        if t == SQOFF:
            legs.append((f"EOD {t}", open_q, c, t))
            events.append({"key": "EXIT_EOD", "time": t, "price": c})
            closed, exit_t = True, t
            break

    if not closed:
        last_c, last_t = float(bars["close"].iloc[-1]), str(bars["t"].iloc[-1])
        legs.append((f"OPEN {last_t}", open_q, last_c, last_t))

    pnl = sum(s * (px - entry) * q for _lbl, q, px, _t in legs)
    risk_rs = risk * qty
    r_total = pnl / risk_rs if risk_rs else 0.0
    # --- human-readable exit path for the report
    parts = []
    for lbl, q, px, t in legs:
        if lbl.startswith("T1"):
            parts.append(f"50% TP@1:{TP1_R:g} {t}")
        elif lbl.startswith("T2"):
            parts.append(f"30% TP@1:{TP2_R:g} {t}")
        elif lbl.startswith("OPEN"):
            parts.append("OPEN")
        else:
            pct = round(100 * q / qty)
            kind = "trail" if lbl.startswith("trail") else ("SL" if lbl.startswith("SL") else "EOD")
            parts.append(f"{pct}% {kind} {t}")
    exit_text = " · ".join(parts) if parts else ""
    last_leg_t = legs[-1][3] if legs else None
    return {
        "symbol": sym, "side": side, "time": etime, "signal": signal, "setup": cls,
        "entry": round(entry, 2), "sl": round(sl, 2), "sl_anchor": anchor,
        "risk_pts": round(risk, 2), "risk_pct": round(risk_pct, 3), "risk_rs": round(risk_rs, 0),
        "qty": qty, "qty_full": qty_full, "qty_capped": qty < qty_full,
        "capital": round(qty * entry, 0),
        "tp1": round(tp1, 2), "tp2": round(tp2, 2), "trail_armed": armed,
        "trail_style": TRAIL_LABEL[cls],
        "legs": legs, "exit_text": exit_text,
        "leg2_time": last_leg_t,          # report-compat: final exit time col
        "pnl": round(pnl, 0), "r_total": round(r_total, 2), "closed": closed,
        "events": events,
    }


def fmt_alert(tr, key):
    arrow = "🟢" if tr["side"] == "BUY" else "🔴"
    setup = SETUP_LABEL.get(tr.get("setup", ""), "")
    base = f"<b>{tr['symbol']}</b> {arrow} {tr['side']} · {tr['signal']}"
    if key == "ENTRY":
        cap = f" (capped from {tr['qty_full']} for ₹1,000 max-loss rule)" if tr.get("qty_capped") else ""
        return (f"🚨 ENTRY · {base}\n"
                f"Setup: <b>{setup}</b> · trail: {tr.get('trail_style', '-')}\n"
                f"Time {tr['time']} · ₹{tr['entry']} · Qty {tr['qty']}{cap} (₹{tr['capital']:,.0f})\n"
                f"SL ₹{tr['sl']} ({tr['sl_anchor']} · max loss ₹{tr['risk_rs']:,.0f})\n"
                f"T1 50% @ ₹{tr['tp1']} (1:1.5) · T2 30% @ ₹{tr['tp2']} (1:2.5) · rest 20% trails · SL→BE after T1")
    if key == "TP1":
        return f"💰 T1 BOOKED · {base}\n50% @ ₹{tr['tp1']} (+1.5R) · SL now floored at breakeven · rest rides T2/trail"
    if key == "TP2":
        return f"💰💰 T2 BOOKED · {base}\n30% @ ₹{tr['tp2']} (+2.5R) · last 20% now on {tr.get('trail_style', 'trail')}"
    if key == "TRAIL_ON":
        return f"🧲 TRAIL ARMED · {base}\n+1R printed — SL now trails {tr.get('trail_style', 'structure')}"
    if key == "EXIT_SL":
        lbl = "TRAIL EXIT" if tr.get("trail_armed") else "EXIT SL"
        return (f"⛔ {lbl} · {base}\n@ ₹{tr['legs'][-1][2]} {tr['legs'][-1][3]} · "
                f"P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)")
    if key == "EXIT_EOD":
        return f"🏁 EXIT 15:20 · {base}\n@ ₹{tr['legs'][-1][2]} · P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)"
    return base
