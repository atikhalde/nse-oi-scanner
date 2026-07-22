#!/usr/bin/env python3
"""Self-test for setup-aware exits v2 (trader.py). Run: python3 test_trader.py"""
import pandas as pd
import trader

ok = True

def check(name, cond):
    global ok
    print(("PASS " if cond else "FAIL ") + name)
    ok &= cond


def mk_bars(rows, warmup=None, start="09:15"):
    """rows: list of (o,h,l,c). Builds today's df with 5-min stamps from 09:15."""
    out = []
    hh, mm = 9, 15
    for i, (o, h, l, c) in enumerate(rows):
        t = f"{hh:02d}:{mm:02d}"
        mm += 5
        if mm >= 60: hh, mm = hh + 1, mm - 60
        out.append({"t": t, "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(out)


def warm(n=120, base=100.0, drift=0.05):
    """Flat-ish uptrend warmup for EMA/ATR seeding."""
    px = base
    rows = []
    for _ in range(n):
        px += drift
        rows.append({"open": px - 0.3, "high": px + 0.4, "low": px - 0.5, "close": px})
    return pd.DataFrame(rows)


# ---------- A. BREAKOUT + ₹1,000 max-loss cap (qty shrink, SL at structure)
# warmup ~100..106; day gaps up: structure pivot low 105.00 -> SL ~104.98; entry 110
day = mk_bars([
    (106.0, 106.8, 105.6, 106.5),   # 09:15
    (106.5, 107.0, 106.0, 106.8),   # 09:20
    (106.8, 107.2, 105.0, 107.0),   # 09:25  low prints 105
    (107.0, 107.3, 105.1, 107.1),   # 09:30
    (107.1, 107.2, 105.2, 107.1),   # 09:35  <- pivot low confirmed (105.0 @09:25 with 2 bars each side? k=2, k+2=4 ok)
    (107.1, 110.4, 109.5, 110.0),   # 09:40  break to new 20-bar high -> entry bar
    (110.0, 111.0, 109.6, 110.2),
    (110.2, 112.0, 110.0, 111.5),
    (111.5, 115.5, 111.0, 115.0),
    (115.0, 122.0, 114.0, 121.0),   # hits T1 (2R) inside
    (121.0, 121.5, 113.0, 113.5),   # pullback
    (113.5, 127.0, 113.2, 126.5),   # hits T2 (3R)
    (126.5, 126.8, 117.0, 117.5),   # drops below trail stop
    (117.5, 118.0, 115.0, 115.5),
])
day.loc[day.index[-1], "t"] = "15:20"   # ensure the square-off bar exists in the synthetic day
tr = trader.evaluate("TEST", "BUY", "09:40", 110.0, "TEST-BUY", day, warmup=warm())
check("A: setup is BREAKOUT", tr.get("setup") == "BREAKOUT")
check("A: SL at structure ~104.98", abs(tr["sl"] - 105.0 * (1 - 0.0002)) < 0.02)
risk = 110.0 - tr["sl"]
qf = 50000 // 110
check("A: qty shrunk by cap", tr["qty"] == max(1, int(900 // risk)) and tr["qty"] < qf)
check("A: planned loss <= Rs900", tr["qty"] * risk <= 900.0001)
check("A: T1 and T2 booked", any(l[0].startswith("T1") for l in tr["legs"]) and any(l[0].startswith("T2") for l in tr["legs"]))
check("A: trail armed after +1R", tr["trail_armed"] and any(e["key"] == "TRAIL_ON" for e in tr["events"]))
check("A: closed via trail/SL or EOD", tr["closed"])
check("A: pnl positive", tr["pnl"] > 0)

# ---------- B. SL checked FIRST inside a bar
dayB = mk_bars([
    (100.0, 100.5, 99.5, 100.0),
    (100.0, 100.4, 99.0, 100.0),
    (100.0, 110.5, 98.5, 109.0),    # entry bar
    (109.0, 109.9, 90.0, 92.0),     # BOTH tp-level high and deep SL breach -> SL first
    (92.0, 93.0, 91.0, 92.0),
])
trB = trader.evaluate("TESTB", "BUY", "09:25", 109.0, "TEST", dayB, warmup=warm(120, 96.0, 0.01))
check("B: SL-first convention (loss trade)", trB["pnl"] < 0)
check("B: exit label SL", trB["legs"][-1][0].startswith(("SL", "trail")))
check("B: no T1 booked", not any(l[0].startswith("T1") for l in trB["legs"]))

# ---------- C. EOD square-off
dayC = mk_bars([
    (100.0, 100.3, 99.7, 100.0),
    (100.0, 100.2, 99.8, 100.1),   # entry
    (100.1, 100.4, 99.9, 100.2),
])
dayC["t"] = ["15:05", "15:10", "15:20"]
trC = trader.evaluate("TESTC", "BUY", "15:10", 100.1, "TEST", dayC, warmup=warm(120, 99, 0.0))
check("C: EOD 15:20 exit", trC["closed"] and "EOD" in trC["legs"][-1][0])
check("C: qty=full, uncapped (tiny risk)", not trC["qty_capped"])

# ---------- D. PULLBACK classification + tight SL
w = warm(140, 108.0, 0.08)          # rising, so EMA20(5m) trails below price
e20_end = w["close"].ewm(span=20, adjust=False).mean().iloc[-1]
dayD = mk_bars([
    (e20_end + 1.0, e20_end + 3.0, e20_end + 0.9, e20_end + 1.2),   # early spike (high anchors hi20)
    (e20_end + 1.3, e20_end + 1.4, e20_end + 1.0, e20_end + 1.2),
    (e20_end + 1.2, e20_end + 1.3, e20_end + 1.1, e20_end + 1.25),
    (e20_end + 1.25, e20_end + 1.4, e20_end + 0.2, e20_end + 1.3),  # dip tags EMA20 zone
    (e20_end + 1.3, e20_end + 1.45, e20_end + 1.1, e20_end + 1.4),  # entry: holds above EMA20
    (e20_end + 1.4, e20_end + 1.6, e20_end + 1.2, e20_end + 1.5),
])
trD = trader.evaluate("TESTD", "BUY", dayD["t"].iloc[4], float(dayD["close"].iloc[4]), "TEST", dayD, warmup=w)
check("D: setup PULLBACK", trD.get("setup") == "PULLBACK")
pb_low = dayD["low"].iloc[0:5].min() * (1 - 0.0002)
check("D: SL ~ pullback low", abs(trD["sl"] - pb_low) < max(0.03, 0.1 * trD["risk_pts"]))
check("D: tight risk (< structure)", trD["risk_pct"] < 1.5)

# ---------- E. determinism
trA2 = trader.evaluate("TEST", "BUY", "09:40", 110.0, "TEST-BUY", day, warmup=warm())
check("E: deterministic re-eval", trA2["pnl"] == tr["pnl"] and trA2["legs"] == tr["legs"])

# ---------- F. SELL mirror works
dayF = mk_bars([
    (104.0, 104.4, 103.6, 104.0),
    (104.0, 104.2, 103.8, 103.9),
    (103.9, 104.0, 96.5, 97.0),   # breakdown -> entry 97
    (97.0, 97.2, 94.0, 94.5),     # falls to T1(2R)? pivot-based risk needed
    (94.5, 95.0, 93.5, 94.0),
])
trF = trader.evaluate("TESTF", "SELL", "09:25", 97.0, "TEST", dayF, warmup=warm(120, 106, -0.01))
check("F: SELL trade produces valid SL above entry", trF["sl"] > 97.0)
check("F: SELL pnl sign consistent", (trF["pnl"] > 0) == (trF["r_total"] > 0))

print("\nRESULT:", "ALL CHECKS PASSED" if ok else "FAILURES ABOVE")
raise SystemExit(0 if ok else 1)
