"""Paper-trade state machine — EXACT strategy rules (100% intact).

Entry: master signal (from the untouched engine), fill at signal-bar close.
SL   : last confirmed 5-min pivot low (BUY) / pivot high (SELL) -/+ 0.02% buffer;
       fallback session extreme before entry when no pivot exists.
Exit : 50% at 1:2 ; remaining 50% trails after 1:3 prints (lock +2R, trail 1R
       behind the extreme) ; SL anytime (checked first inside a bar) ;
       square-off at 15:20 bar close.
Sizing: qty = floor(CAPITAL / entry) with CAPITAL = 50000.

evaluate() is DETERMINISTIC over the full bar set -> safe to re-run every cycle;
alerting dedups via event keys.
"""
CAPITAL = 50000
BUFFER = 0.0002
SQOFF = "15:20"


def swing_sl(bars, ei, side):
    lows, highs, ts = bars["low"].values, bars["high"].values, bars["t"].values
    piv = None
    for i in range(2, ei - 2):
        if side == "BUY" and lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            piv = (lows[i], ts[i])
        if side == "SELL" and highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
            piv = (highs[i], ts[i])
    if piv:
        raw, at = piv
        return (raw * (1 - BUFFER) if side == "BUY" else raw * (1 + BUFFER)), f"pivot {at}"
    raw = lows[:ei].min() if side == "BUY" else highs[:ei].max()
    return (raw * (1 - BUFFER) if side == "BUY" else raw * (1 + BUFFER)), "session-extreme*"


def evaluate(sym, side, etime, entry, signal, bars):
    """bars: full day 5-min df with 't' column. Returns dict trade state + events list."""
    ei_list = bars.index[bars["t"] == etime].tolist()
    if not ei_list:
        return {"symbol": sym, "error": "entry bar missing"}
    ei = ei_list[0]
    sl, anchor = swing_sl(bars, ei, side)
    R = (entry - sl) if side == "BUY" else (sl - entry)
    if R <= 0:
        return {"symbol": sym, "error": "SL beyond entry"}
    qty = int(CAPITAL // entry)
    tp1 = entry + 2 * R if side == "BUY" else entry - 2 * R
    trig = entry + 3 * R if side == "BUY" else entry - 3 * R
    stop, tp1_done, trig_done = sl, False, False
    leg1 = leg2 = None
    run_hi, run_lo = entry, entry
    events = [{"key": "ENTRY", "time": etime, "price": entry}]

    for j in range(ei + 1, len(bars)):
        o = float(bars.loc[j, "open"]); h = float(bars.loc[j, "high"])
        l = float(bars.loc[j, "low"]); c = float(bars.loc[j, "close"]); t = bars.loc[j, "t"]
        if side == "BUY" and l <= stop:
            px = o if o < stop else stop
            leg1 = leg1 or (px, t, "SL")
            leg2 = (px, t, "SL")
            events.append({"key": "EXIT_SL", "time": t, "price": px})
            break
        if side == "SELL" and h >= stop:
            px = o if o > stop else stop
            leg1 = leg1 or (px, t, "SL")
            leg2 = (px, t, "SL")
            events.append({"key": "EXIT_SL", "time": t, "price": px})
            break
        if not trig_done and ((side == "BUY" and h >= trig) or (side == "SELL" and l <= trig)):
            trig_done = True
            events.append({"key": "TRAIL_ON", "time": t, "price": float(h if side == "BUY" else l)})
        if trig_done:
            if side == "BUY":
                run_hi = max(run_hi, h); stop = max(stop, run_hi - R)
            else:
                run_lo = min(run_lo, l); stop = min(stop, run_lo + R)
        if not tp1_done and ((side == "BUY" and h >= tp1) or (side == "SELL" and l <= tp1)):
            tp1_done = True
            leg1 = (tp1, t, "TP1@1:2")
            events.append({"key": "TP1", "time": t, "price": tp1})
        if t == SQOFF:
            leg2 = (c, t, "EOD15:20")
            events.append({"key": "EXIT_EOD", "time": t, "price": c})
            break

    closed = leg2 is not None
    if closed is False:
        # still open: mark current mtm
        last_c = float(bars["close"].iloc[-1]); last_t = bars["t"].iloc[-1]
        leg2 = (last_c, last_t, "OPEN")
    if leg1 is None:
        leg1 = leg2 if leg2[2] != "OPEN" else (None, None, "OPEN")

    def rmult(px):
        return ((px - entry) if side == "BUY" else (entry - px)) / R
    closed_r1 = rmult(leg1[0]) if leg1[0] is not None else None
    r2 = rmult(leg2[0])
    sign = 1 if side == "BUY" else -1
    pnl = sign * (((leg1[0] if leg1[0] is not None else leg2[0]) + leg2[0]) / 2 - entry) * qty
    return {
        "symbol": sym, "side": side, "time": etime, "signal": signal, "entry": round(entry, 2),
        "sl": round(sl, 2), "sl_anchor": anchor, "risk_pts": round(R, 2), "risk_pct": round(R / entry * 100, 3),
        "qty": qty, "capital": round(qty * entry, 0),
        "tp1": round(tp1, 2), "trigger13": round(trig, 2), "trail_on": trig_done,
        "leg1_px": leg1[0], "leg1_time": leg1[1], "leg1_why": leg1[2],
        "leg2_px": leg2[0], "leg2_time": leg2[1], "leg2_why": leg2[2],
        "r_total": round(((closed_r1 if closed_r1 is not None else r2) + r2) / 2, 2),
        "pnl": round(pnl, 0), "closed": closed, "events": events,
    }


def fmt_alert(tr, key):
    arrow = "🟢" if tr["side"] == "BUY" else "🔴"
    base = f"<b>{tr['symbol']}</b> {arrow} {tr['side']} · {tr['signal']}"
    if key == "ENTRY":
        return (f"🚨 ENTRY · {base}\n"
                f"Time {tr['time']} · ₹{tr['entry']} · Qty {tr['qty']} (₹{tr['capital']:,.0f})\n"
                f"SL ₹{tr['sl']} ({tr['sl_anchor']}) · 1:2 ₹{tr['tp1']} · trail arms ₹{tr['trigger13']}")
    if key == "TP1":
        return f"💰 TP1 HIT · {base}\n50% booked @ ₹{tr['tp1']} · stop stays ₹{tr['sl']} on rest"
    if key == "TRAIL_ON":
        return f"🧲 TRAIL ON · {base}\n+3R printed — rest now trails (locked ≥ +2R)"
    if key == "EXIT_SL":
        return f"⛔ EXIT SL · {base}\n@ ₹{tr['leg2_px']} {tr['leg2_time']} · P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)"
    if key == "EXIT_EOD":
        return f"🏁 EXIT 15:20 · {base}\n@ ₹{tr['leg2_px']} · P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)"
    return base
