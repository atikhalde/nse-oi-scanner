#!/usr/bin/env python3
"""Unit tests for m10_runner (Coach-v2.1: whitelist · breadth side-gate · confirm)."""
import m10_runner as M


def ok(label):
    print("PASS", label)


def run():
    # --- stage 2 whitelist (unchanged from v2)
    for c in (102, 104, 105, 106, 111):
        assert M.coach_v2_why("BUY", c) is None, c
    for c in (101, 108, 103):
        assert M.coach_v2_why("BUY", c) is not None, c
    for c in (208, 212, 213, 219, 220):
        assert M.coach_v2_why("SELL", c) is None, c
    for c in (201, 203, 250):
        assert M.coach_v2_why("SELL", c) is not None, c
    ok("whitelist: 5 BUY + 5 SELL codes in, churn codes out")

    # --- stage 2.1 breadth side-gate: blocks ONLY the counter-trend side
    assert M.coach_v3_side("SELL", "BULL") is not None
    assert M.coach_v3_side("BUY", "BULL") is None
    assert M.coach_v3_side("BUY", "BEAR") is not None
    assert M.coach_v3_side("SELL", "BEAR") is None
    assert M.coach_v3_side("BUY", "NEUTRAL") is None and M.coach_v3_side("SELL", "NEUTRAL") is None
    ok("side-gate: BULL blocks SELL · BEAR blocks BUY · NEUTRAL passes both (not strict)")

    # --- stage 3 momentum/fade confirm
    assert M.coach_v3_confirm("BUY", 500.0, 480.0, "BULL") is None        # green BUY ok
    assert M.coach_v3_confirm("BUY", 470.0, 480.0, "BULL") is not None    # red BUY blocked
    assert M.coach_v3_confirm("BUY", 470.0, 480.0, "NEUTRAL") is not None
    assert M.coach_v3_confirm("SELL", 500.0, 480.0, "NEUTRAL") is None    # fade ok in chop
    assert M.coach_v3_confirm("SELL", 470.0, 480.0, "NEUTRAL") is not None  # red short blocked in chop
    assert M.coach_v3_confirm("SELL", 470.0, 480.0, "BEAR") is None       # momentum short ok on bear tape
    assert M.coach_v3_confirm("SELL", 500.0, 480.0, "BEAR") is None       # fade also ok (lenient, tagged)
    assert M.coach_v3_confirm("BUY", 500.0, None, "BULL") is not None     # pc unknown -> strict
    ok("confirm: BUY needs green · SELL green in chop / free in bear · pc None strict")

    # --- regime hysteresis (2-cycle anti-flap; lenient fallback)
    st = {"regime": "NEUTRAL", "regime_raw": None, "regime_cnt": 0}
    assert M.regime_update(st, 61.0) == "NEUTRAL"     # 1st bull cycle: no flip yet
    assert M.regime_update(st, 60.0) == "BULL"        # 2nd consecutive: flips
    assert M.regime_update(st, 50.0) == "BULL"        # dip to neutral: stays (hysteresis)
    assert M.regime_update(st, 50.0) == "NEUTRAL"     # 2nd neutral cycle: back to neutral
    assert M.regime_update(st, None) == "NEUTRAL"     # breadth unknown -> NEUTRAL permissive
    assert M.regime_update(st, 40.0) == "NEUTRAL"
    assert M.regime_update(st, 39.0) == "BEAR"        # 2nd bear cycle -> flips BEAR
    ok("hysteresis: 2-cycle flip · unknown breadth = NEUTRAL permissive")

    # --- v2.2 sector breadth = TELEMETRY ONLY (no veto); thin reads -> permissive None
    import pandas as _pd
    M.SEC_OF.clear(); M.SEC_MEMBERS.clear()
    M.SEC_OF.update({f"S{i}": "TESTSEC" for i in range(1, 6)} | {"KAYNES": "CAPGOODS"})
    M.SEC_MEMBERS["TESTSEC"] = [f"S{i}" for i in range(1, 6)]
    def _bars(close):
        return _pd.DataFrame({"close": [close], "dt": [_pd.Timestamp("2026-08-03 10:00")]})
    stx = {"pcmap": {"S1": 100, "S2": 100, "S3": 100, "S4": 100, "S5": 100}}
    bm = {"S1": _bars(105), "S2": _bars(101), "S3": _bars(99), "S4": _bars(95), "S5": _bars(100)}  # 2 up /2 dn /1 flat
    p, s, n = M.sector_adv(stx, bm, "2026-08-03", "S1")
    assert s == "TESTSEC" and n == 5 and p == 50.0, (p, s, n)
    bm2 = {"S1": _bars(105), "S2": _bars(101)}                       # only 2 fed -> thin
    p2, s2, n2 = M.sector_adv(stx, bm2, "2026-08-03", "S1")
    assert p2 is None and s2 == "TESTSEC" and n2 == 2, (p2, s2, n2)
    assert M.sector_adv(stx, bm, "2026-08-03", "NOSUCHSYM") == (None, None, 0)  # unmapped
    ok("sector telemetry: 5-member 50% read · <3 fed -> None · unmapped -> None (never vetoes)")

    # --- alert formatter smoke
    tr = {"symbol": "KAYNES", "side": "BUY", "signal": "BUY-EX17", "time": "09:55",
          "entry": 3376.0, "qty": 14, "capital": 47264.0, "sl": 3360.1,
          "sl_mode": "structure", "risk_rs": 900.0, "coach_via": "buy-102",
          "pc": 3250.0, "daypct": 3.88, "regime": "BULL", "adv": 62.5,
          "sec": "CAPGOODS", "sec_adv": 66.7,
          "legs": [["T1 10:15", 7, 3450.0, "10:15"]], "pnl": 600.0,
          "exit_text": "50% T1", "closed": True, "status": "CLOSED"}
    a = M.fmt_m10_alert("🅼10", tr, "ENTRY")
    assert "🅼10 ENTRY" in a and "BULL" in a and "62.5" in a
    assert "🅼10" in M.fmt_m10_alert("🅼10", tr, "EXIT_T1")
    ok("alert formatter entry (regime+adv shown) + exit smoke")

    print("\nALL M10 TESTS PASSED ✅")


if __name__ == "__main__":
    run()
