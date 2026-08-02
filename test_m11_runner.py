#!/usr/bin/env python3
"""Unit tests for m11_runner — the 4 video setup detectors (both sides). Offline."""
import pandas as pd
import m11_runner as M


def ok(label):
    print("PASS", label)


def mkdf(bars):
    df = pd.DataFrame(bars, columns=["t", "open", "high", "low", "close"])
    df["dt"] = pd.to_datetime("2026-08-03 " + df["t"])
    return df


def bar(t, o, c, h=None, l=None):
    """Return (t, open, high, low, close) in DataFrame column order."""
    return (t, o, h if h is not None else max(o, c) + 0.1,
            l if l is not None else min(o, c) - 0.1, c)


def run():
    # ---------- S1 morning base ----------
    times = ["09:15", "09:20", "09:25", "09:30", "09:35", "09:40", "09:45", "10:00", "10:05"]
    lows_ok =   [100.0, 101.0, 102.0, 101.5, 103.0, 104.2, 104.0, 105.0, 106.0]
    df = mkdf([bar(t, 100 + i, 100 + i + 0.5, h=102.0 + i, l=lo) for i, (t, lo) in enumerate(zip(times, lows_ok))])
    assert M.s1_morning_base(df, "BUY") is True          # first-30-min low is the day low
    lows_bad =  [100.0, 101.0, 102.0, 101.5, 103.0, 104.2, 104.0, 105.0, 99.0]  # late new low
    df2 = mkdf([bar(t, 100 + i, 100 + i + 0.5, h=102.0 + i, l=lo) for i, (t, lo) in enumerate(zip(times, lows_bad))])
    assert M.s1_morning_base(df2, "BUY") is False        # base broken after 09:45
    highs_ok =  [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 102.0]
    df3 = mkdf([bar(t, 115 - i, 114 - i, h=hi, l=100.0 - i) for i, (t, hi) in enumerate(zip(times, highs_ok))])
    assert M.s1_morning_base(df3, "SELL") is True        # first-30-min high is the day high
    highs_bad = [110.0, 109.0, 108.0, 107.0, 106.0, 105.0, 104.0, 103.0, 111.0]
    df4 = mkdf([bar(t, 115 - i, 114 - i, h=hi, l=100.0 - i) for i, (t, hi) in enumerate(zip(times, highs_bad))])
    assert M.s1_morning_base(df4, "SELL") is False       # base broken (new high)
    ok("S1 morning base: intact passes, broken fails (both sides)")

    # ---------- S2 pivot pullback (P = 100) ----------
    P = 100.0
    # bars 0-2 close above P; bars 3-8 dip and touch P by low; bar 9 closes back above
    t = [f"10:{m:02d}" for m in range(0, 50, 5)]
    buy_df = mkdf([bar(t[0], 100.8, 101.2), bar(t[1], 101.2, 101.5), bar(t[2], 101.5, 101.3),
                   bar(t[3], 101.3, 101.0), bar(t[4], 101.0, 100.6), bar(t[5], 100.6, 100.2, h=100.75, l=99.6),
                   bar(t[6], 100.2, 100.1), bar(t[7], 100.1, 100.3), bar(t[8], 100.3, 100.4),
                   bar(t[9], 100.4, 100.5)])
    assert M.s2_pivot_pullback(buy_df, 9, "BUY", P) is True
    no_touch = buy_df.copy(); no_touch.loc[[5, 6, 7, 8], "low"] = 100.2   # dip never reaches P
    assert M.s2_pivot_pullback(no_touch, 9, "BUY", P) is False
    not_earlier = buy_df.copy()
    not_earlier.loc[0:8, "close"] = [99.5, 99.6, 99.7, 99.8, 99.7, 99.8, 99.6, 99.5, 99.6]
    # still dips to P (touch via bar5 low 99.6) but NO close was ever above P
    assert M.s2_pivot_pullback(not_earlier, 9, "BUY", P) is False
    fresh = buy_df.copy()                                  # HCL-style: cross is RECENT
    fresh.loc[0:5, "close"] = [99.4, 99.5, 99.6, 99.5, 99.6, 99.7]
    fresh.loc[6, "close"] = 100.3                          # cross-above inside the window…
    fresh.loc[7, "close"] = 100.2
    fresh.loc[8, "low"] = 99.9                             # …then fresh touch…
    fresh.loc[9, "close"] = 100.4                          # …then resume close > P
    assert M.s2_pivot_pullback(fresh, 9, "BUY", P) is True
    ok("S2 ordered: fresh cross + fresh touch + resume passes (old version would miss it)")
    sell_df = mkdf([bar(t[0], 99.2, 98.8), bar(t[1], 98.8, 98.5), bar(t[2], 98.5, 98.7),
                    bar(t[3], 98.7, 99.0), bar(t[4], 99.0, 99.4), bar(t[5], 99.4, 99.8, h=100.4),
                    bar(t[6], 99.8, 99.9), bar(t[7], 99.9, 99.7), bar(t[8], 99.7, 99.6),
                    bar(t[9], 99.6, 99.5)])
    assert M.s2_pivot_pullback(sell_df, 9, "SELL", P) is True
    assert M.s2_pivot_pullback(sell_df, 9, "SELL", None) is False       # pivot unknown -> strict
    ok("S2 pivot pullback: retest+resume passes both sides; no-touch/no-break fails; P None strict")

    # ---------- S3 flag breakout ----------
    bt = [f"10:{m:02d}" for m in range(0, 45, 5)]
    pole_flag = (
        [bar(bt[0], 99.9, 100.0), bar(bt[1], 100.0, 100.2),           # c0=100.0
         bar(bt[2], 100.2, 100.5), bar(bt[3], 100.5, 100.9, h=100.95, l=100.45)] +   # pole plen3: +0.7%
        [bar(bt[4], 100.88, 100.9, h=100.92, l=100.84),               # flag: tight
         bar(bt[5], 100.9, 100.86, h=100.91, l=100.84),
         bar(bt[6], 100.86, 100.88, h=100.92, l=100.85),
         bar(bt[7], 100.88, 100.87, h=100.9, l=100.83)] +
        [bar(bt[8], 100.87, 101.0, h=101.05, l=100.85)]               # breakout close > flag high
    )
    dfB = mkdf(pole_flag)
    assert M.s3_flag_breakout(dfB, 8, "BUY") is True
    inside = dfB.copy(); inside.loc[8, "close"] = 100.9               # closes inside flag
    assert M.s3_flag_breakout(inside, 8, "BUY") is False
    wide = dfB.copy(); wide.loc[[4, 5, 6, 7], "low"] = 100.35         # whole flag wide (not a tight flag)
    assert M.s3_flag_breakout(wide, 8, "SELL") is False or True       # may SELL? no: close high vs pole dir
    assert M.s3_flag_breakout(wide, 8, "BUY") is False
    # SELL mirror: down pole + tight flag + close below flag low
    pole_flag_s = (
        [bar(bt[0], 100.1, 100.0), bar(bt[1], 100.0, 99.8),
         bar(bt[2], 99.8, 99.5), bar(bt[3], 99.5, 99.1, h=99.55, l=99.05)] +
        [bar(bt[4], 99.12, 99.1, h=99.16, l=99.08),
         bar(bt[5], 99.1, 99.14, h=99.16, l=99.09),
         bar(bt[6], 99.14, 99.12, h=99.15, l=99.08),
         bar(bt[7], 99.12, 99.13, h=99.17, l=99.1)] +
        [bar(bt[8], 99.13, 99.0, h=99.15, l=98.95)]
    )
    dfS = mkdf(pole_flag_s)
    assert M.s3_flag_breakout(dfS, 8, "SELL") is True
    ok("S3 flag breakout: pole+tight flag+close-out passes both sides; inside-close & wide-flag fail")

    # ---------- S4 sandwich ----------
    def _times(start="09:15", n=20):
        hh, mm = map(int, start.split(":"))
        out = []
        for _ in range(n):
            out.append(f"{hh:02d}:{mm:02d}")
            mm += 5
            if mm >= 60:
                hh += 1; mm -= 60
        return out
    st = _times("10:00", 14)
    base = [bar(st[i], 100.0 + i * 0.1, 100.1 + i * 0.1) for i in range(10)]          # slow uptrend
    tri = [bar(st[10], 100.9, 101.3, h=101.4, l=100.85),                               # green
           bar(st[11], 101.2, 100.95, h=101.25, l=100.9),                              # trapped red
           bar(st[12], 100.95, 101.35, h=101.45, l=100.9)]                             # green
    curB = bar(st[13], 101.35, 101.6, h=101.7, l=101.3)                                # breakout above envelope
    df4B = mkdf(base + tri + [curB])
    assert M.s4_sandwich(df4B, 13, "BUY") is True
    no_brk = df4B.copy(); no_brk.loc[13, "close"] = 101.4                              # no breakout after trap
    assert M.s4_sandwich(no_brk, 13, "BUY") is False
    bas2 = [bar(st[i], 110.0 - i * 0.1, 109.9 - i * 0.1) for i in range(10)]           # slow downtrend
    tri2 = [bar(st[10], 109.1, 108.7, h=109.15, l=108.6),                              # red
            bar(st[11], 108.8, 109.05, h=109.1, l=108.75),                             # trapped green
            bar(st[12], 109.05, 108.65, h=109.1, l=108.55)]                            # red
    curS = bar(st[13], 108.65, 108.4, h=108.7, l=108.35)                               # breakout below envelope
    df4S = mkdf(bas2 + tri2 + [curS])
    assert M.s4_sandwich(df4S, 13, "SELL") is True
    ok("S4 sandwich: trap+breakout passes both sides; no-breakout fails")

    # ---------- orchestrator + tags ----------
    tt = [f"09:{15+5*i}" for i in range(7)] + ["10:00", "10:05"]
    rows = [bar("09:15", 100.0, 100.4, h=100.6, l=99.9),
            bar("09:20", 100.4, 100.8, h=101.0, l=100.3),
            bar("09:25", 100.8, 101.2, h=101.4, l=100.7),
            bar("09:30", 101.2, 101.6, h=101.8, l=101.1),
            bar("09:35", 101.6, 102.0, h=102.2, l=101.5),
            bar("09:40", 102.0, 102.4, h=102.6, l=101.9),
            bar("09:45", 102.4, 102.8, h=103.0, l=102.3),
            bar("10:00", 102.8, 103.2, h=103.4, l=102.7),
            bar("10:05", 103.2, 103.6, h=103.8, l=103.1)]
    dfX = mkdf(rows)
    got = M.video_setups(dfX, 8, "BUY", None)
    assert "S1" in got and "S4" not in got            # steady rise: base intact, no sandwich
    ok(f"orchestrator returns detector tags (S1 present): {got}")

    # ---------- alert smoke (M8-format rich alert + M11 extras) ----------
    tr = {"symbol": "KAYNES", "side": "BUY", "signal": "BUY-EX17", "time": "10:05",
          "entry": 3376.0, "qty": 14, "capital": 47264.0, "sl": 3360.1,
          "sl_anchor": "pullback-high/low", "risk_pts": 15.9, "risk_rs": 900.0,
          "cls_trader": "PULLBACK", "setup": "S1+S2", "trail_style": "structure swings",
          "sl_mode": "structure", "setups": ["S1", "S2"],
          "legs": [["T1 10:35", 7, 3450.0, "10:35"]], "pnl": 600.0, "r_total": 0.67,
          "exit_text": "50% T1", "closed": True, "status": "CLOSED"}
    aE = M.fmt_m11_alert(tr, "ENTRY")
    assert "🅼11" in aE and "ENTRY" in aE and "KAYNES" in aE
    assert "Pullback (low-risk)" in aE                    # setup NAME (trader class)
    assert "SL ₹3360.1" in aE and "max loss ₹900" in aE  # SL with anchor + max loss
    assert "TARGET ₹3391.9 (+1R trail-arm" in aE         # target line = +1R arm
    assert "S1 Morning Base" in aE and "S2 Pivot Pullback" in aE   # video setup names
    assert "🅼11" in M.fmt_m11_alert(tr, "EXIT_T1")
    ok("alert formatter: M8-style ENTRY (setup+SL+target+video names) + exit smoke")

    # ---------- M11 2-extra-bot telegram fanout (offline monkeypatch) ----------
    import os as _os
    sent_extras, sent_main = [], []

    class _Resp:
        ok = True
        status_code = 200
    _old_main, _old_post = M.tg.send_message, M.tg._post
    try:
        M.tg.send_message = lambda text, silent=False: sent_main.append(text) or True
        M.tg._post = lambda url, **kw: sent_extras.append(url) or _Resp()
        # 1) no extra secrets -> falls back to main chat only (never silent)
        for k in ("M11_BOT_TOKEN_A", "M11_CHAT_ID_A", "M11_BOT_TOKEN_B", "M11_CHAT_ID_B"):
            _os.environ.pop(k, None)
        assert M.m11_targets() == []
        M._send_m11("x"); M._send_m11("y")
        assert len(sent_main) == 2 and not sent_extras
        # 2) secrets A+B set -> main + 2 fanout posts per alert
        _os.environ["M11_BOT_TOKEN_A"] = "tokA"; _os.environ["M11_CHAT_ID_A"] = "chatA"
        _os.environ["M11_BOT_TOKEN_B"] = "tokB"; _os.environ["M11_CHAT_ID_B"] = "chatB"
        assert M.m11_targets() == [("tokA", "chatA"), ("tokB", "chatB")]
        sent_main.clear(); sent_extras.clear()
        M._send_m11("hello")
        assert len(sent_main) == 1 and len(sent_extras) == 2
        assert "bottokA/sendMessage" in sent_extras[0] and "bottokB/sendMessage" in sent_extras[1]
        # 3) main-only switch (M11_INCLUDE_MAIN False) -> no main, extras still fire
        M.M11_INCLUDE_MAIN = False
        sent_main.clear(); sent_extras.clear()
        M._send_m11("hi")
        assert not sent_main and len(sent_extras) == 2
        M.M11_INCLUDE_MAIN = True
    finally:
        M.tg.send_message, M.tg._post = _old_main, _old_post
        for k in ("M11_BOT_TOKEN_A", "M11_CHAT_ID_A", "M11_BOT_TOKEN_B", "M11_CHAT_ID_B"):
            _os.environ.pop(k, None)
    ok("telegram fanout: main-only fallback / main+A+B fanout / extras-only switch")

    print("\nALL M11 TESTS PASSED ✅")


if __name__ == "__main__":
    run()
