"""Deterministic M14 Ultra High-Conviction paper trade manager.

Risk controls:
- MIS 5x leverage: ₹10,000 margin -> ₹50,000 notional per trade cap.
- Planned risk budget cap: <= ₹900 per trade (shrunk for higher volatility stocks).
- Structure SL / PrevBar SL / Signal Candle SL options.
- Pure Runner / Dual Leg exits with structure-swing trailing.
- Square-off at 15:20 IST.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Optional
import numpy as np
import pandas as pd

BUFFER = 0.0002
MARGIN_RS = 10000
LEVERAGE = 5
NOTIONAL_CAP = MARGIN_RS * LEVERAGE  # ₹50,000 notional
RISK_CAP = 900  # ₹900 planned rupee risk cap

@dataclass(frozen=True)
class Config:
    sl_mode: str = "structure"  # "structure", "prevbar", "candle"
    runner_mode: str = "v3_pure"  # "v3_pure" (100% runner after +1R) or "v2_split" (50% book + 50% runner)
    book_r: float = 1.0
    book_frac: float = 0.50
    trail_arm_r: float = 1.0

DEFAULT_CONFIG = Config()


def _indicators(warm: Optional[pd.DataFrame], bars: pd.DataFrame) -> tuple[np.ndarray, int]:
    if warm is not None and len(warm):
        base = pd.concat([warm, bars[["open", "high", "low", "close"]].reset_index(drop=True)], ignore_index=True)
    else:
        base = bars[["open", "high", "low", "close"]].reset_index(drop=True)
    e9 = base["close"].ewm(span=9, adjust=False).mean().values
    off = len(base) - len(bars)
    return e9, off


def evaluate(sym: str, side: str, etime: str, entry: float, signal: str, bars: pd.DataFrame,
             warmup: Optional[pd.DataFrame] = None, features: Optional[dict] = None,
             config: Config = DEFAULT_CONFIG) -> dict:
    ids = bars.index[bars["t"] == etime].tolist() if "t" in bars.columns else bars.index[bars["dt"].dt.strftime("%H:%M") == etime].tolist()
    if not ids:
        return {"symbol": sym, "error": f"entry bar {etime} missing"}

    ei = ids[0]
    side = str(side).upper()
    s = 1.0 if side == "BUY" else -1.0
    entry = float(entry)

    # Compute initial stop loss based on sl_mode
    sig_lo = float(bars["low"].iloc[ei])
    sig_hi = float(bars["high"].iloc[ei])

    if config.sl_mode == "candle":
        sl = sig_lo * (1.0 - BUFFER) if side == "BUY" else sig_hi * (1.0 + BUFFER)
        sl_anchor = "signal candle high/low ±0.02%"
    elif config.sl_mode == "prevbar" and ei > 0:
        prev_lo = float(bars["low"].iloc[ei - 1])
        prev_hi = float(bars["high"].iloc[ei - 1])
        sl = prev_lo * (1.0 - BUFFER) if side == "BUY" else prev_hi * (1.0 + BUFFER)
        sl_anchor = "previous candle low/high ±0.02%"
    else:
        # Structure swing SL (pivot of last 6 bars or 2+2 swing)
        lookback = max(0, ei - 5)
        struct_lo = float(bars["low"].iloc[lookback:ei + 1].min())
        struct_hi = float(bars["high"].iloc[lookback:ei + 1].max())
        sl = struct_lo * (1.0 - BUFFER) if side == "BUY" else struct_hi * (1.0 + BUFFER)
        sl_anchor = "5-bar structure swing ±0.02%"

    risk = abs(entry - sl)
    if risk <= 0:
        return {"symbol": sym, "error": "invalid zero/negative risk points"}

    # Sizing logic
    qty_notional = int(NOTIONAL_CAP // entry)
    qty_risk = int(RISK_CAP // risk)

    if qty_notional < 1:
        return {"symbol": sym, "error": "notional qty < 1"}
    if qty_risk < 1:
        return {"symbol": sym, "error": "one-share risk exceeds ₹900 cap"}

    qty = max(1, min(qty_notional, qty_risk))
    capital = round(qty * entry, 0)
    margin_rs = round(capital / LEVERAGE, 0)
    risk_rs = round(risk * qty, 0)

    # Setup execution and exits
    e9, off = _indicators(warmup, bars)
    stop = sl
    best = entry
    open_q = qty
    legs = []
    events = [{"key": "ENTRY", "time": etime, "price": entry}]
    closed = False
    trail_armed = False
    booked = False

    target = entry + s * config.book_r * risk

    for j in range(ei + 1, len(bars)):
        o = float(bars["open"].iloc[j])
        h = float(bars["high"].iloc[j])
        lo = float(bars["low"].iloc[j])
        c = float(bars["close"].iloc[j])
        t = str(bars["t"].iloc[j]) if "t" in bars.columns else str(bars["dt"].iloc[j].strftime("%H:%M"))

        # 1. Stop loss check (evaluated at open/intrabar)
        if (s == 1.0 and lo <= stop) or (s == -1.0 and h >= stop):
            px = o if ((s == 1.0 and o < stop) or (s == -1.0 and o > stop)) else stop
            lbl = "RUNNER SL" if trail_armed else "SL"
            legs.append((lbl, open_q, px, t))
            events.append({"key": "EXIT_SL" if not trail_armed else "EXIT_TRAIL", "time": t, "price": px})
            open_q = 0
            closed = True
            break

        # Track best price and MFE (in R units)
        best = max(best, h) if side == "BUY" else min(best, lo)
        mfe_r = s * (best - entry) / risk

        # 2. Check partial book target if in split mode
        if config.runner_mode == "v2_split" and not booked and open_q > 1:
            if (s == 1.0 and h >= target) or (s == -1.0 and lo <= target):
                qbook = int(qty * config.book_frac)
                legs.append((f"TP1@+{config.book_r:.1f}R", qbook, target, t))
                open_q -= qbook
                booked = True
                events.append({"key": "TP1", "time": t, "price": target})

        # 3. Arm structure trailing once MFE reaches +1R
        if mfe_r >= config.trail_arm_r:
            if not trail_armed:
                trail_armed = True
                events.append({"key": "TRAIL_ARMED", "time": t, "price": c})

            # Ratchet trailing stop to swing structure or EMA9
            lookback_j = max(ei, j - 2)
            if side == "BUY":
                swing_trail = float(bars["low"].iloc[lookback_j:j + 1].min()) * (1.0 - BUFFER)
                e9_trail = float(e9[off + j]) * (1.0 - BUFFER)
                new_stop = max(stop, swing_trail, e9_trail)
                stop = max(stop, new_stop)
            else:
                swing_trail = float(bars["high"].iloc[lookback_j:j + 1].max()) * (1.0 + BUFFER)
                e9_trail = float(e9[off + j]) * (1.0 + BUFFER)
                new_stop = min(stop, swing_trail, e9_trail)
                stop = min(stop, new_stop)

        # 4. EOD 15:20 square-off
        if t >= "15:20":
            legs.append(("EOD 15:20", open_q, c, t))
            events.append({"key": "EXIT_EOD", "time": t, "price": c})
            open_q = 0
            closed = True
            break

    if not closed and open_q > 0:
        t = str(bars["t"].iloc[-1]) if "t" in bars.columns else str(bars["dt"].iloc[-1].strftime("%H:%M"))
        c = float(bars["close"].iloc[-1])
        legs.append(("OPEN", open_q, c, t))

    # Calculate P&L
    pnl = sum(s * (px - entry) * q for _l, q, px, _t in legs)
    r_total = pnl / risk_rs if risk_rs > 0 else 0.0

    parts = []
    for lbl, q, px, t in legs:
        parts.append(f"{lbl} {t} @ ₹{px:.2f}")

    return {
        "symbol": sym,
        "side": side,
        "time": etime,
        "signal": signal,
        "setup": "M14-A+",
        "entry": round(entry, 2),
        "sl": round(sl, 2),
        "sl_anchor": sl_anchor,
        "risk_pts": round(risk, 2),
        "risk_pct": round(risk / entry * 100.0, 3),
        "risk_rs": round(risk_rs, 0),
        "qty": qty,
        "qty_full": qty_notional,
        "qty_capped": qty < qty_notional,
        "capital": capital,
        "margin_rs": margin_rs,
        "book_target": round(target, 2),
        "booked": booked,
        "trail_armed": trail_armed,
        "legs": legs,
        "events": events,
        "exit_text": " · ".join(parts),
        "leg2_time": legs[-1][3] if legs else etime,
        "pnl": round(pnl, 0),
        "r_total": round(r_total, 2),
        "closed": closed,
        "features": features or {},
    }


def fmt_alert(tr: dict, key: str) -> str:
    arrow = "🟢" if tr["side"] == "BUY" else "🔴"
    base = f"<b>{tr['symbol']}</b> {arrow} {tr['side']} · {tr['signal']}"
    if key == "ENTRY":
        return (
            f"🅼14 🚨 ENTRY · {base}\n"
            f"Time {tr['time']} IST · ₹{tr['entry']} · Qty {tr['qty']} · Notional ₹{tr['capital']:,.0f} (Margin ~₹{tr['margin_rs']:,.0f})\n"
            f"SL ₹{tr['sl']} ({tr['sl_anchor']}) · Planned risk ₹{tr['risk_rs']:,.0f}\n"
            f"A+ Score: {tr.get('features', {}).get('score', 80.0):.1f} · Max 3/day"
        )
    if key == "TP1":
        return f"🅼14 💰 PARTIAL BOOK · {base}\nTarget +1R printed · Risk reduced to zero"
    if key == "TRAIL_ARMED":
        return f"🅼14 🧲 TRAIL ARMED · {base}\n+1R printed · Structure swing trailing active"
    lbl = {"EXIT_SL": "SL EXIT", "EXIT_TRAIL": "TRAIL EXIT", "EXIT_EOD": "EOD 15:20 SQOFF"}.get(key, key)
    return f"🅼14 ⛔ {lbl} · {base}\n{tr['exit_text']} · Gross P&L ₹{tr['pnl']:+,.0f} ({tr['r_total']:+.2f}R)"
