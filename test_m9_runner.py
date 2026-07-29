#!/usr/bin/env python3
"""Unit tests for m9_runner.evaluate_m9 — the M9 cash data-farm paper sim.
Run: python3 test_m9_runner.py   (script-style, like test_trader.py)
"""
import pandas as pd

import m9_runner as M


def mk_bars(closes, lows=None, highs=None, opens=None, start="09:30"):
    """Build today's-bar df with 5-min stamps from a close list."""
    n = len(closes)
    lows = lows or [c - 1.0 for c in closes]
    highs = highs or [c + 1.0 for c in closes]
    opens = opens or closes[:]
    hh, mm = int(start[:2]), int(start[3:])
    ts = [f"{(hh * 60 + mm + 5 * i) // 60:02d}:{(hh * 60 + mm + 5 * i) % 60:02d}" for i in range(n)]
    dts = pd.date_range(f"2026-07-29 {start}", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"dt": dts, "t": ts, "open": opens, "high": highs,
                         "low": lows, "close": closes})


def test_sl_intrabar():
    # entry 100 @09:30 (entry-bar low 99 → SL 98.9802); 09:35 low 98.50 <= SL -> fill at SL
    b = mk_bars([100, 100.5], lows=[99.0, 98.50], highs=[101, 101], opens=[100, 100.4])
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    assert tr["closed"] and tr["exit_text"].startswith("100% SL 09:35"), tr["exit_text"]
    sl = 99.0 * (1 - M.SL_BUF)
    assert abs(tr["legs"][-1][2] - sl) < 1e-9, tr["legs"]
    exp = (sl - 100.0) * tr["qty"]
    assert abs(tr["pnl"] - exp) < 1.5, (tr["pnl"], exp)
    print("PASS sl intrabar")


def test_sl_gap_through_open():
    # 09:35 OPENS below the SL -> fill at the open, not the stop
    b = mk_bars([100, 98.0], lows=[99.0, 97.5], highs=[101, 98.2], opens=[100, 97.9])
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    assert tr["legs"][-1][1] and tr["legs"][-1][2] == 97.9, tr["legs"]
    assert abs(tr["legs"][-1][2] - 97.9) < 1e-9
    print("PASS sl gap-through fills at open")


def test_signal_bar_exempt_and_ema5_exit():
    # gentle slide that never touches the SL (entry-bar low 99 → SL 98.98):
    # closes dip under the falling EMA5 on bar j=3 while lows stay above the stop.
    closes = [100, 99.6, 99.4, 99.2]
    lows = [99.0, 99.4, 99.2, 99.0]                      # all > SL 98.98 → no SL
    b = mk_bars(closes, lows=lows, highs=[c + 0.2 for c in closes])
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    e5, off = M._ema5(None, b)
    jexp = next(j for j in range(1, len(closes)) if closes[j] < e5[off + j])
    assert tr["closed"] and tr["exit_text"] == f"100% EMA5 {b['t'].iloc[jexp]}", tr["exit_text"]
    assert abs(tr["legs"][-1][2] - closes[jexp]) < 1e-9
    # and the signal bar itself never triggers even if it closes < ema5
    assert tr["legs"][-1][3] != "09:30"
    print(f"PASS ema5 trail exit @ {tr['exit_text']}")


def test_eod_sqoff():
    # calm rising tape: no SL, no close < EMA5 -> forced exit at the 15:20 close
    closes, ts = [], []
    hh, mm = 9, 30
    for i in range(71):          # 09:30 .. 15:20
        tmin = hh * 60 + mm + 5 * i
        ts.append(f"{tmin // 60:02d}:{tmin % 60:02d}")
        closes.append(100 + 0.3 * i)
    dts = pd.date_range("2026-07-29 09:30", periods=len(closes), freq="5min", tz="Asia/Kolkata")
    b = pd.DataFrame({"dt": dts, "t": ts, "open": closes,
                      "high": [c + 0.05 for c in closes],
                      "low": [c - 0.05 for c in closes], "close": closes})
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    assert tr["closed"] and tr["exit_text"] == "100% EOD 15:20", tr["exit_text"]
    assert abs(tr["legs"][-1][2] - closes[-1]) < 1e-9
    print("PASS 15:20 square-off")


def test_open_leg_when_bars_end():
    b = mk_bars([100, 100.5, 101.0])   # bars stop before any exit trigger
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    assert not tr["closed"] and tr["exit_text"] == "OPEN"
    assert tr["legs"][-1][0].startswith("OPEN")
    print("PASS open mtm leg")


def test_qty_and_risk_cap():
    # entry 4,000 with entry-bar low 3,990 -> risk ≈ 10.8 -> 50k notional = 12 shares
    # -> risk 129.6 <= 900, full size. Wide candle (low 3,900) -> risk ≈ 100.8/sh
    # -> qty capped at floor(900/100.8) = 8.
    b = mk_bars([4000, 4005], lows=[3990, 3995])
    tr = M.evaluate_m9("X", "09:30", 4000.0, "BUY-EX", b)
    assert tr["qty"] == 12 and not tr["qty_capped"], (tr["qty"], tr["qty_capped"])
    b2 = mk_bars([4000, 4005], lows=[3900, 3995])
    tr2 = M.evaluate_m9("X", "09:30", 4000.0, "BUY-EX", b2)
    assert tr2["qty"] == 8 and tr2["qty_capped"], (tr2["qty"], tr2["qty_capped"])
    assert tr2["risk_rs"] <= 900 + tr2["risk_pts"]   # planned max loss honoured
    print("PASS sizing: 50k notional + Rs900 risk cap")


def test_sl_never_on_entry_bar():
    # entry bar itself dips below its own low? impossible; but ensure the loop
    # starts strictly AFTER the entry bar even when ema5 < entry close immediately.
    b = mk_bars([100, 100.2], lows=[99.0, 99.9])
    tr = M.evaluate_m9("X", "09:30", 100.0, "BUY-EX", b)
    assert tr["events"][0]["key"] == "ENTRY" and tr["events"][0]["time"] == "09:30"
    print("PASS entry-bar exemption")


def test_missing_entry_bar_rejected():
    b = mk_bars([100, 100.5])
    tr = M.evaluate_m9("X", "10:50", 100.0, "BUY-EX", b)   # etime not in bars
    assert "error" in tr and tr["error"] == "entry bar missing"
    print("PASS missing entry bar rejected (defensive)")


def test_warmup_continuity():
    # EMA5 must be seeded from warmup (continuous series), not cold-started today:
    # with a falling history, the first post-entry close below the seeded EMA5 exits.
    warm_cl = [110 - 0.5 * i for i in range(30)]
    warm = pd.DataFrame({"open": warm_cl, "high": warm_cl, "low": warm_cl, "close": warm_cl})
    b = mk_bars([96.0, 96.2])                # entry 96, next close 96.2
    tr = M.evaluate_m9("X", "09:30", 96.0, "BUY-EX", b, warmup=warm)
    e5, off = M._ema5(warm, b)
    seeded = e5[off + 1]
    assert seeded > 96.2, f"seeded ema5 {seeded} should sit above 96.2 after falling history"
    assert tr["closed"] and tr["exit_text"].startswith("100% EMA5"), tr["exit_text"]
    print("PASS ema5 warmup continuity")


if __name__ == "__main__":
    test_sl_intrabar()
    test_sl_gap_through_open()
    test_signal_bar_exempt_and_ema5_exit()
    test_eod_sqoff()
    test_open_leg_when_bars_end()
    test_qty_and_risk_cap()
    test_sl_never_on_entry_bar()
    test_missing_entry_bar_rejected()
    test_warmup_continuity()
    print("\nALL M9 TESTS PASSED ✅")
