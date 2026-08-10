#!/usr/bin/env python3
"""Unit tests for the standalone M12 entry logic."""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

import m12_entry as E


def ok(name, cond):
    assert cond, name
    print("PASS", name)


def test_decisions():
    good = dict(dir_prev_pct=0.12, ema9_20_atr=0.10, ema20_50_atr=-0.50,
                close_ema20_atr=1.0, dir_gap_pct=-0.30,
                sector_breadth_prev_dir=0.40,
                video_setup_count=1, video_setups="S1", clock_relvol=1.2)
    d = E.decide(102, "BUY-EX17", "BUY", "10:00", good)
    ok("valid whitelisted anti-chase candidate passes", d.accepted and d.score >= 70)
    ok("current SELL code map includes EX8 offset", E.ALLOWED_CODES[209] == "SELL-EX8")
    ok("code/name mismatch fails closed",
       not E.decide(209, "SELL-EX7", "SELL", "10:00", good).accepted)
    chase = dict(good, dir_prev_pct=0.201)
    ok("directional chase is rejected", not E.decide(102, "BUY-EX17", "BUY", "10:00", chase).accepted)
    crowded = dict(good, sector_breadth_prev_dir=0.431)
    ok("crowded sector direction is rejected",
       not E.decide(102, "BUY-EX17", "BUY", "10:00", crowded).accepted)
    no_video = dict(good, video_setup_count=0, video_setups="")
    ok("at least one causal video setup is mandatory",
       not E.decide(102, "BUY-EX17", "BUY", "10:00", no_video).accepted)
    low_volume = dict(good, clock_relvol=0.99)
    ok("same-clock relative volume confirmation is mandatory",
       not E.decide(102, "BUY-EX17", "BUY", "10:00", low_volume).accepted)
    ok("EMA structure remains ranking telemetry, not an unproven veto",
       E.decide(102, "BUY-EX17", "BUY", "10:00",
                dict(good, ema9_20_atr=9.0, ema20_50_atr=9.0)).accepted)
    ok("unapproved signal family rejects", not E.decide(101, "BUY-EX", "BUY", "10:00", good).accepted)
    ok("entry window is strict", not E.decide(102, "BUY-EX17", "BUY", "09:25", good).accepted)
    missing = dict(good, dir_prev_pct=None)
    ok("missing causal input fails closed", not E.decide(102, "BUY-EX17", "BUY", "10:00", missing).accepted)


def test_cap_policy():
    ok("hard maximum is five", E.MAX_TRADES_PER_DAY == 5)
    ok("no delayed slot-reservation helper exists", not hasattr(E, "slot_cap"))
    ok("late/backfilled signals have a strict freshness ceiling", E.MAX_SIGNAL_AGE_MIN == 5.0)


def test_feature_causality():
    idx = pd.date_range("2026-07-20 09:15", periods=100, freq="5min", tz="Asia/Kolkata")
    close = np.linspace(100, 101, len(idx))
    frame = pd.DataFrame({"open": close - .03, "high": close + .08,
                          "low": close - .08, "close": close,
                          "volume": np.full(len(idx), 1000)}, index=idx)
    today = frame.iloc[-20:].reset_index(names="dt")
    prefix = today.iloc[:10]
    engine_prefix = frame.loc[:prefix["dt"].iloc[-1]]
    a = E.causal_price_features(engine_prefix, prefix, "BUY", 100.5, .4)
    # Mutating bars after the signal cannot change a feature when the caller passes the
    # required timestamp prefix (the runner does this for every catch-up bar).
    future = frame.copy(); future.loc[future.index > prefix["dt"].iloc[-1], "close"] = 999
    b = E.causal_price_features(future.loc[:prefix["dt"].iloc[-1]], prefix, "BUY", 100.5, .4)
    ok("future bars do not affect timestamp-prefix features", a == b)


def test_timestamp_breadth():
    dtidx = pd.date_range("2026-07-20 09:30", periods=3, freq="5min", tz="Asia/Kolkata")
    def bars(vals):
        return pd.DataFrame({"dt": dtidx, "close": vals})
    bm = {"A": bars([101, 102, 50]), "B": bars([99, 98, 50]), "C": bars([101, 102, 150])}
    pc = {"A": 100, "B": 100, "C": 100}; sm = {"A": "X", "B": "X", "C": "X"}
    early = E.sector_breadth_at(bm, pc, sm, "X", "BUY", dtidx[0])
    late = E.sector_breadth_at(bm, pc, sm, "X", "BUY", dtidx[2])
    ok("sector breadth uses the signal timestamp, not newest feed bar",
       abs(early - 2 / 3) < 1e-9 and abs(late - 1 / 3) < 1e-9)
    sell = E.sector_breadth_at(bm, pc, sm, "X", "SELL", dtidx[0])
    ok("SELL breadth mirrors BUY breadth", abs(sell - 1 / 3) < 1e-9)
    ctx_early = E.market_sector_context_at(bm, pc, sm, "A", "X", "BUY", dtidx[0])
    ctx_late = E.market_sector_context_at(bm, pc, sm, "A", "X", "BUY", dtidx[2])
    ok("market bullish/bearish breadth is timestamp-causal",
       abs(ctx_early["market_adv_ratio"]-2/3)<1e-9 and abs(ctx_late["market_adv_ratio"]-1/3)<1e-9)
    ok("sector advance/decline and lead/lag telemetry is recorded",
       "sector_adv_ratio" in ctx_early and "stock_leads_sector" in ctx_early and
       "sector_lags_market" in ctx_early)


def test_three_bot_fanout():
    import os
    import m12_runner as R
    keys = [f"{p}_{k}_{s}" for p in ("M12", "M11")
            for k in ("BOT_TOKEN", "CHAT_ID") for s in ("A", "B")]
    saved = {k: os.environ.get(k) for k in keys}
    old_main, old_post, old_doc = R.tg.send_message, R.tg._post, R.tg.send_document
    calls = []
    try:
        for k in keys:
            os.environ.pop(k, None)
        R.tg.send_message = lambda text, silent=False: calls.append(("main", text, silent))
        R.tg._post = lambda url, **kw: calls.append(("extra", url, kw))
        R.tg.send_document = lambda path, caption="": calls.append(("main-doc", path, caption))
        ok("M12 fanout falls back to main-only when no extra pairs exist",
           R._send_m12("x") == 1 and len(calls) == 1)
        calls.clear()
        for s in ("A", "B"):
            os.environ[f"M11_BOT_TOKEN_{s}"] = f"fallback-token-{s}"
            os.environ[f"M11_CHAT_ID_{s}"] = f"fallback-chat-{s}"
        ok("M12 reuses complete M11 A/B pairs for three targets",
           R._send_m12("x") == 3 and len(calls) == 3)
        calls.clear()
        os.environ["M12_BOT_TOKEN_A"] = "m12-token-A"
        os.environ["M12_CHAT_ID_A"] = "m12-chat-A"
        ok("M12-specific pair overrides only the matching fallback pair",
           R._send_m12("x") == 3 and len(calls) == 3 and
           any("m12-token-A" in str(x) for x in calls))
        calls.clear()
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
            ok("M12 documents fan out to all three targets",
               R._doc_m12(f.name, "report") == 3 and len(calls) == 3)
    finally:
        R.tg.send_message, R.tg._post, R.tg.send_document = old_main, old_post, old_doc
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_strict_alert_registry():
    import json
    import m12_runner as R
    old_state = R.STATE
    try:
        with tempfile.TemporaryDirectory() as td:
            R.STATE = Path(td) / "state12.json"
            st = {"alerts": []}
            ok("first alert reservation succeeds and is persisted before send",
               R.reserve_alert_once(st, "T:ENTRY") and
               "T:ENTRY" in json.loads(R.STATE.read_text())["alerts"])
            ok("second reservation of the same alert is strictly rejected",
               not R.reserve_alert_once(st, "T:ENTRY") and st["alerts"].count("T:ENTRY") == 1)
            owned = R.reserve_alert_batch(st, ["EOD:SUMMARY", "EOD:DOC", "T:ENTRY"])
            ok("batch reservation owns only previously unseen alerts",
               owned == {"EOD:SUMMARY", "EOD:DOC"} and st["alerts"].count("T:ENTRY") == 1)
            ok("replayed EOD batch owns nothing and cannot resend",
               not R.reserve_alert_batch(st, ["EOD:SUMMARY", "EOD:DOC"]))
    finally:
        R.STATE = old_state


def test_previous_close_freshness():
    import json
    import m12_runner as R
    old_cache, old_hist, old_syms = R.PREV_CACHE, R.L.HIST, R.L.SYMS
    try:
        with tempfile.TemporaryDirectory() as td:
            R.PREV_CACHE = Path(td) / "prev.json"
            R.L.HIST = Path(td) / "empty-history"
            R.L.HIST.mkdir()
            vals = {f"S{i}": 100 + i for i in range(180)}
            R.L.SYMS = list(vals)
            R.PREV_CACHE.write_text(json.dumps({"date": "2026-08-06", "close": vals}))
            got, meta = R.load_previous_closes("2026-08-07")
            ok("fresh previous-close cache passes", meta["status"] == "OK" and len(got) == 180)
            R.PREV_CACHE.write_text(json.dumps({"date": "2026-07-30", "close": vals}))
            got, meta = R.load_previous_closes("2026-08-07")
            ok("stale previous-close cache fails closed", not got and meta["status"] == "STALE")
    finally:
        R.PREV_CACHE, R.L.HIST, R.L.SYMS = old_cache, old_hist, old_syms


def main():
    test_decisions(); test_cap_policy(); test_feature_causality(); test_timestamp_breadth()
    test_three_bot_fanout(); test_strict_alert_registry(); test_previous_close_freshness()
    print("ALL M12 ENTRY TESTS PASSED")


if __name__ == "__main__":
    main()
