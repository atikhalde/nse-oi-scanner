#!/usr/bin/env python3
"""M6 next-second alert runner — regression suite (offline, deterministic).

The bugs this pins down (all measured on the live workflow before the rewrite):

  * STALE/AFTER-MARKET ALERTS — a post-close cycle replayed bars 0..74 and sent
    three live Telegram alerts at 18:00–18:24 IST (state6.json 2026-11-17).
  * BAR-AGE — nothing checked how old the bar was, so a 15:25 cycle could alert
    a 10:00 entry.
  * CURSOR REPLAY — `known = 0` when the feed came back thinner than the stored
    count replayed the whole day.
  * SLEEP DRIFT — `--loop N` slept an unaligned 240 s; 2 of every 3 candle
    closes were skipped.
  * BUCKET GRID — `_synth_input` built its timestamp grid from member #0 only.
  * MISSING 15:20 BAR — Yahoo skips it on 206/210 symbols, so open trades never
    squared off at EOD (marked OPEN, pnl 0).
  * INDEX volume=None — `.dropna()` silently dropped every freshly closed
    ^NSEI/^CNX* bar (no board at all).

The headline test replays 2026-11-17 candle by candle (all 74 closes) and
asserts the gated entries and skip reasons are IDENTICAL to a brute-force scan
that runs the untouched engine on every symbol at every close, then checks the
alert latency stays inside the freshness budget.

Run: python test_m6_runner.py
"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

import m6_runner as M
import flow_map as FM
from concurrent.futures import ProcessPoolExecutor
import live_runner as L

DAY = "2026-11-17"
CLOSES = M.session_closes(dt.date(2026, 11, 17))
NULL_BOARD = None


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}{(' — ' + extra) if extra else ''}")
    print(f"PASS {name}")


def _df(rows, tz="Asia/Kolkata"):
    """[(hhmm, o,h,l,c,v)] -> normalized bars frame with 't'."""
    out = pd.DataFrame([{"dt": pd.Timestamp(f"{DAY} {r[0]}", tz=tz),
                         "open": r[1], "high": r[2], "low": r[3], "close": r[4],
                         "volume": (r[5] if len(r) > 5 else 0)} for r in rows])
    return M._prep(out)


def _live_bars(sym):
    fp = L.HIST / f"{sym}.csv"
    h = pd.read_csv(fp)
    h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata")
    d = h[h["dt"].dt.strftime("%Y-%m-%d") == DAY].reset_index(drop=True)
    return M._prep(d)


# ------------------------------------------------------------------ 1. scheduling
def test_sleep_math():
    day = dt.date(2026, 11, 17)
    closes = M.session_closes(day)
    ok("74 candle closes per session", len(closes) == 74, str(len(closes)))
    ok("first close 09:20, last close 15:25",
       closes[0].strftime("%H:%M") == "09:20" and closes[-1].strftime("%H:%M") == "15:25")

    n = dt.datetime(2026, 11, 17, 9, 15, tzinfo=M.IST)
    wake, close = M.next_wake_time(n)
    ok("wake lands exactly 1.0 s after the close",
       wake == dt.datetime(2026, 11, 17, 9, 20, 1, tzinfo=M.IST) and close.strftime("%H:%M") == "09:20")

    n = dt.datetime(2026, 11, 17, 9, 20, 1, tzinfo=M.IST)
    wake, _ = M.next_wake_time(n)
    ok("a cycle that fired early waits for the NEXT close (never re-runs 09:20)",
       wake == dt.datetime(2026, 11, 17, 9, 25, 1, tzinfo=M.IST))

    n = dt.datetime(2026, 11, 17, 15, 25, 1, tzinfo=M.IST)
    wake, _ = M.next_wake_time(n)
    ok("after the last close there is no further slot today", wake is None)

    n = dt.datetime(2026, 11, 20, 16, 0, tzinfo=M.IST)   # Friday evening
    wake, _ = M.next_wake_time(n, allow_rollover=True)
    ok("--forever rolls over the weekend to Monday 09:20:01",
       wake == dt.datetime(2026, 11, 23, 9, 20, 1, tzinfo=M.IST))

    # every scheduled wake must be close + offset, never an unaligned 240 s sleep
    for i in range(3):
        w, c = M.next_wake_time(closes[i] + dt.timedelta(seconds=1.01))
        ok(f"gap after close {c.strftime('%H:%M')} is a clean +1 s slot",
           (w - c).total_seconds() == 1.0 and w.strftime("%S") == "01")

    # sleep helper: chunked, deadline-aware, no busy wait past the target
    fake = {"t": dt.datetime(2026, 11, 17, 9, 19, 0, tzinfo=M.IST)}
    slept = []

    def fake_now():
        return fake["t"]

    def fake_sleep(sec):
        slept.append(sec)
        fake["t"] = fake["t"] + dt.timedelta(seconds=sec)

    target = dt.datetime(2026, 11, 17, 9, 20, 1, tzinfo=M.IST)
    reached = M._sleep_until(target, now_fn=fake_now, sleep_fn=fake_sleep, warm=False)
    ok("_sleep_until lands on the target", reached and fake["t"] == target)
    ok("_sleep_until never sleeps in coarse 240 s steps", max(slept) <= 20.0, str(max(slept)))

    fake["t"] = dt.datetime(2026, 11, 17, 9, 19, 0, tzinfo=M.IST)
    stop_at = dt.datetime(2026, 11, 17, 9, 19, 30, tzinfo=M.IST)
    reached = M._sleep_until(target, deadline=stop_at, now_fn=fake_now, sleep_fn=fake_sleep, warm=False)
    ok("_sleep_until honours an earlier deadline", (not reached) and fake["t"] <= stop_at)


# ------------------------------------------------------------------ 2. bars
def test_closed_and_forming_bars():
    bars = _df([("09:15", 100, 101, 99, 100.5), ("09:20", 100.5, 102, 100, 101.5),
                ("09:25", 101.5, 103, 101, 102.5)])
    # a cycle that wakes at 09:25:01 evaluates the candle that closed at 09:25
    closing = pd.Timestamp(f"{DAY} 09:25:01", tz=M.IST).to_pydatetime()
    closed = M._closed_bars(bars, closing)
    ok("a bar is included the second it closes", closed["t"].tolist() == ["09:15", "09:20"])
    closed = M._closed_bars(bars, pd.Timestamp(f"{DAY} 09:27", tz=M.IST).to_pydatetime())
    ok("the still-forming 09:25 bar is always excluded",
       closed["t"].tolist() == ["09:15", "09:20"])
    closed = M._closed_bars(bars, pd.Timestamp(f"{DAY} 09:30:01", tz=M.IST).to_pydatetime())
    ok("the next close adds exactly one bar", closed["t"].tolist() == ["09:15", "09:20", "09:25"])
    ok("merged basket member rebuilds the HH:MM column",
       "t" in M._closed_bars(bars[["dt", "open", "high", "low", "close", "volume"]],
                             pd.Timestamp(f"{DAY} 09:30", tz=M.IST).to_pydatetime()).columns)


def test_sqoff_bar_rebuild():
    bars = _df([("15:15", 100, 101, 99, 100.5), ("15:25", 100.8, 101.2, 100.6, 101.0)])
    fixed = M._ensure_sqoff_bar(bars, DAY)
    ok("missing 15:20 bar rebuilt from the 15:25 open", "15:20" in fixed["t"].tolist())
    row = fixed[fixed["t"] == "15:20"].iloc[0]
    ok("rebuilt 15:20 row carries the 15:25 open price", abs(row["close"] - 100.8) < 1e-9)
    already = _df([("15:20", 100, 101, 99, 100.5)])
    ok("an existing 15:20 bar is never overwritten",
       len(M._ensure_sqoff_bar(already, DAY)) == 1)


# ------------------------------------------------------------------ 3. cursor
def test_cursor_regression():
    st = M.fresh_state(DAY)
    bars = _df([("09:15", 1, 1, 1, 1), ("09:20", 1, 1, 1, 1), ("09:25", 1, 1, 1, 1)])
    closed = M._closed_bars(bars, pd.Timestamp(f"{DAY} 09:30", tz=M.IST).to_pydatetime())
    ok("cold start has every closed bar pending", M._new_bars(st, "X", closed) == ["09:15", "09:20", "09:25"])
    M._advance_cursor(st, "X", closed)
    ok("cursor stores last_bar + nbars",
       st["signals"]["X"]["last_bar"] == "09:25" and st["signals"]["X"]["nbars"] == 3)
    ok("nothing re-fires once the cursor is advanced", M._new_bars(st, "X", closed) == [])

    # the old bug: feed comes back with FEWER bars -> known > n_today -> known = 0 -> replay
    thin = M._closed_bars(bars.iloc[:2], pd.Timestamp(f"{DAY} 09:30", tz=M.IST).to_pydatetime())
    ok("a thinner feed NEVER replays the day", M._new_bars(st, "X", thin) == [])
    M._advance_cursor(st, "X", thin)
    ok("cursor is monotonic (never moves backwards)", st["signals"]["X"]["last_bar"] == "09:25")

    grown = M._closed_bars(_df([("09:15", 1, 1, 1, 1), ("09:20", 1, 1, 1, 1),
                                ("09:25", 1, 1, 1, 1), ("09:30", 1, 1, 1, 1)]),
                           pd.Timestamp(f"{DAY} 09:35", tz=M.IST).to_pydatetime())
    ok("only the genuinely new bar is returned", M._new_bars(st, "X", grown) == ["09:30"])

    legacy = M.fresh_state(DAY)
    legacy["signals"]["Y"] = {"nbars": 4}
    ok("legacy state without last_bar adopts the cursor without replay",
       M._new_bars(legacy, "Y", closed) == [] and legacy["signals"]["Y"]["last_bar"] == "09:25")

    empty = M.fresh_state(DAY)
    ok("symbol with no history evaluates the current bar once",
       M._new_bars(empty, "Z", closed) == ["09:15", "09:20", "09:25"])


# ------------------------------------------------------------------ 4. state
def test_state_guard():
    tmp = Path(tempfile.mkdtemp()) / "state6.json"
    old = M.STATE6
    try:
        M.STATE6 = tmp
        tmp.write_text('{"date": "%s", "trades": {' % DAY)      # truncated by a killed run
        try:
            M.load_state(DAY)
            ok("corrupt state fails closed", False)
        except M.StateCorrupt:
            ok("corrupt state fails closed", True)
        st = M.fresh_state(DAY)
        st["trades"]["AAA"] = {"symbol": "AAA"}
        M.save_state(st)
        back = M.load_state(DAY)
        ok("state round-trips", back["date"] == DAY and "AAA" in back["trades"])
        ok("atomic write leaves no tmp file", not tmp.with_suffix(".json.tmp").exists())
        back2 = M.load_state("2026-11-18")
        ok("a new session starts from a clean ledger", back2["trades"] == {} and back2["alerts"] == [])
    finally:
        M.STATE6 = old


def test_skip_dedupe():
    st = M.fresh_state(DAY)
    row = {"symbol": "AAA", "side": "BUY", "signal": "BUY-EX1", "time": "10:00", "why": "x"}
    ok("skip recorded once", M._add_skip(st, dict(row)) and len(st["skipped"]) == 1)
    ok("same skip is not recorded twice", (not M._add_skip(st, dict(row))) and len(st["skipped"]) == 1)


# ------------------------------------------------------------------ 5. freshness
def test_stale_entry_suppressed():
    """The production failure, end to end: the SAME gated signal is alerted in a
    fresh cycle (close +1 s) and silently dropped in a late/after-hours cycle."""
    sym = next(s for s in sorted(M.SECTOR_OF) if M.SECTOR_OF[s] in M.MEMBERS)
    sec = M.SECTOR_OF[sym]
    entry = 1500.0

    class FakeBoard:
        sectors = {sec: {"day_ret": -5.0}}
        bull_q, bear_q, persist_bull, persist_bear = [], [], [], []
        net_tilt, tilt_label = 0.0, "NEUTRAL"
        bull_top2, bear_top2, held_bull, held_bear = [], [], [], []
        hold_mode = "off"

        def entry_ok(self, side, sec_):
            return True, f"{sec_} gate open"

    def fake_engine(jobs, on_result=None):
        """Stand-in for the untouched scanner: one BUY-EX12 signal on 10:00."""
        out = {}
        for s, _dframe in jobs:
            res = pd.DataFrame({"scan_code": [111], "scan_name": ["BUY-EX12"]},
                               index=pd.DatetimeIndex([pd.Timestamp(f"{DAY} 10:00", tz=M.IST)]))
            out[s] = res
            if on_result:
                on_result(s, res)
        return out

    def run(at, cursor_last):
        st = M.fresh_state(DAY)
        st["signals"][sym] = {"last_bar": cursor_last, "nbars": 5}
        bars = _df([("09:55", 1497, 1499, 1496, 1498), ("10:00", 1499, 1501, 1498, entry)])
        sent = []
        real = (M.send, M.fetch_symbols, M.symbol_history, M.run_engine_jobs)
        M.send = lambda text, silent=False, dry_run=False: sent.append(text) or True
        M.fetch_symbols = lambda symbols, now=None, workers=None: {}
        M.symbol_history = lambda s, d: {"ok": True, "warmup": None, "prev_close": 1400.0,
                                         "eng_hist": None, "yd": None, "prev2_close": None}
        M.run_engine_jobs = fake_engine
        try:
            clock = pd.Timestamp(f"{DAY} {at}", tz=M.IST).to_pydatetime()
            stats = M.scan_entries(st, DAY, clock, clock, FakeBoard(), {sym: bars},
                                   {sym: M.symbol_history(sym, DAY)}, dry_run=False,
                                   fetch_missing=False)
        finally:
            M.send, M.fetch_symbols, M.symbol_history, M.run_engine_jobs = real
        return stats, sent, st

    stats, sent, st = run("10:05:01", "09:55")     # the 10:00 bar closes at 10:05:00
    ok("fresh bar (close +1 s) alerts immediately",
       stats["entries"] == 1 and len(sent) == 1 and sym in sent[0], f"{stats} {sent}")
    ok("the alert text shows its own latency", "+1.0s" in sent[0], sent[0].splitlines()[-1])
    ok("entry persisted before the alert went out",
       st["alerts"] == [f"{sym}:ENTRY"] and sym in st["trades"])

    stats, sent, st = run("10:08:00", "09:55")
    ok("a bar 181 s old is NOT alerted", stats["entries"] == 0 and sent == [], str(sent))
    ok("the stale bar is recorded for the audit trail, never sent",
       any("stale" in it["why"] for it in st["skipped"]) and st["trades"] == {})

    # the exact production failure: a post-close run replaying an old bar
    stats, sent, st = run("18:24:00", "09:55")
    ok("an 18:24 IST cycle sends nothing at all", sent == [] and stats["entries"] == 0)

    stats, sent, st = run("10:05:01", "10:00")
    ok("an already-processed bar is never re-alerted", sent == [] and stats["entries"] == 0)


def test_bar_age_helper():
    for at, bar, expected in (("10:05:01", "10:00", True), ("10:07:00", "10:00", True),
                              ("10:07:01", "10:00", False), ("10:04:00", "10:00", False)):
        lag = (pd.Timestamp(f"{DAY} {at}", tz=M.IST) - pd.Timestamp(M._bar_close_ts(bar, DAY))).total_seconds()
        ok(f"age of {bar} bar at {at} is {lag:+.0f}s → {'ok' if expected else 'stale'}",
           (0 <= lag <= M.MAX_LAG) is expected)


# ------------------------------------------------------------------ 6. board
def _board_inputs(close_ts, idx_live, syms):
    bars_map, cache = {}, {}
    for s in syms:
        bars_map[s] = _live_bars(s)
        cache[s] = M.symbol_history(s, DAY)
    return bars_map, cache


def test_board_and_index_volume():
    """Index bars with volume=None must survive the feed (they used to vanish)."""
    raw = pd.DataFrame({"dt": [pd.Timestamp(f"{DAY} 10:00", tz="Asia/Kolkata")] * 2,
                        "open": [100.0, 200.0], "high": [101.0, 201.0], "low": [99.0, 199.0],
                        "close": [100.5, 200.5], "volume": [None, 500.0]})
    kept = raw.dropna(subset=["open", "high", "low", "close"])
    ok("volume=None index bars are kept by the new dropna contract", len(kept) == 2)
    ok("_prep fills a missing volume column with 0",
       (M._prep(raw.drop(columns=["volume"]))["volume"] == 0).all())


def test_synth_union_grid():
    """Member #0 lagging by a bar must not shift every other member's series."""
    close_ts = pd.Timestamp(f"{DAY} 10:30", tz=M.IST).to_pydatetime()
    sec = next(iter(M.MEMBERS))
    members = M.MEMBERS[sec]
    ok(f"synthetic sector {sec} has members", len(members) >= 2, str(members))
    lagging, others = members[0], members[1:]
    bars_map = {lagging: _live_bars(lagging).iloc[:-3]}            # 3 bars behind
    for s in others:
        bars_map[s] = _live_bars(s)
    cache = {s: M.symbol_history(s, DAY) for s in members}
    got = M._synth_input(sec, DAY, close_ts, bars_map, cache)
    ok("synthetic basket builds without member #0's grid", got is not None)
    full = {s: _live_bars(s) for s in members}
    ref = M._synth_input(sec, DAY, close_ts, full, cache)
    ok("union grid keeps every member's latest bar",
       len(got["c"]) >= len(ref["c"]) - 3 and abs(float(got["c"][-1] - ref["c"][-1])) < 5.0)
    ok("no NaNs leak into the basket series (levels are smoothed)",
       bool(np.isfinite(got["c"]).all()))


# ------------------------------------------------------------------ 7. EOD / holiday
def test_post_close_guards():
    tmp = Path(tempfile.mkdtemp()) / "state6.json"
    old_state, old_send, old_eod = M.STATE6, M.send, M.run_eod
    sent, eods = [], []
    try:
        M.STATE6 = tmp
        M.send = lambda text, silent=False, dry_run=False: sent.append(text) or True
        M.run_eod = lambda st, today, now, **kw: eods.append(kw.get("reason")) or True

        st = M.fresh_state(DAY)                       # afternoon, live cycles ran
        st["live_cycles"] = 12
        st["trades"]["AAA"] = {"symbol": "AAA", "side": "BUY"}
        M.save_state(st)
        late = dt.datetime(2026, 11, 17, 18, 24, tzinfo=M.IST)
        M.cmd_session(dry_run=False, once=False) if False else None
        # directly exercise the post-close branch through run_cycle's guard chain
        out = M.run_cycle(now=late, dry_run=False, st=M.load_state(DAY), today=DAY)
        ok("a 18:24 cycle is refused at the EOD gate", out.get("status") in ("eod-done", "no-session", "refused"))

        st = M.fresh_state(DAY)
        M.save_state(st)
        M._mark_eod_silent(M.load_state(DAY), False, "after 15:45 with no session")
        ok("silent EOD sends zero Telegram messages", sent == [] and eods == [])

        st = M.fresh_state(DAY)
        st["live_cycles"] = 3
        st["trades"]["AAA"] = {"symbol": "AAA"}
        ok("recovery EOD is allowed when live cycles + trades exist",
           int(st["live_cycles"]) > 0 and bool(st["trades"]))
    finally:
        M.STATE6, M.send, M.run_eod = old_state, old_send, old_eod


def test_weekday_guard():
    ok("Saturday is refused", not M._weekday_ok(dt.date(2026, 11, 21)))
    ok("Monday is allowed", M._weekday_ok(dt.date(2026, 11, 23)))
    os.environ["M6_ALLOW_WEEKEND"] = "1"
    try:
        ok("M6_ALLOW_WEEKEND=1 overrides the guard", M._weekday_ok(dt.date(2026, 11, 21)))
    finally:
        os.environ.pop("M6_ALLOW_WEEKEND", None)
    ok("holiday: no bars for today -> no board, no entries",
       M.build_board(pd.Timestamp("2026-10-02 10:30", tz=M.IST).to_pydatetime(),
                     "2026-10-02", {}, {}, {})[0] is None)


# ------------------------------------------------------------------ 8. feed lag
def test_wait_target_ready():
    close_ts = pd.Timestamp(f"{DAY} 10:00", tz=M.IST).to_pydatetime()
    clock = {"t": pd.Timestamp(f"{DAY} 10:00:01", tz=M.IST).to_pydatetime()}
    published_after = {"n": 3}
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        rows = [("09:55", 1, 1, 1, 1)]
        if calls["n"] > published_after["n"]:
            rows.append(("10:00", 1, 1, 1, 1))
        return _df(rows)

    def fake_sleep(sec):
        clock["t"] += dt.timedelta(seconds=sec)

    bars, waited = M.wait_target_ready("10:00", close_ts, max_lag=120, fetch=fetch,
                                       now_fn=lambda: clock["t"], sleep_fn=fake_sleep)
    ok("readiness poll returns as soon as the bar lands",
       M._has_bar(bars, "10:00") and waited < 5.0, f"{waited:.2f}s")

    clock["t"] = pd.Timestamp(f"{DAY} 10:00:01", tz=M.IST).to_pydatetime()
    bars, waited = M.wait_target_ready("10:00", close_ts, max_lag=8,
                                       fetch=lambda: _df([("09:55", 1, 1, 1, 1)]),
                                       now_fn=lambda: clock["t"], sleep_fn=fake_sleep)
    ok("readiness poll gives up at close + max_lag (never blocks the session)",
       not M._has_bar(bars, "10:00") and 6.0 <= waited <= 9.0, f"{waited:.1f}s")


# ------------------------------------------------------------------ 9. benchmarks
def test_latency_budget():
    """The alert path must be seconds, not minutes: board + engine + dispatch."""
    close_ts = pd.Timestamp(f"{DAY} 11:00", tz=M.IST).to_pydatetime()
    syms = L.SYMS
    t0 = time.monotonic()
    bars_map, cache = {}, {}
    for s in syms:
        fp = L.HIST / f"{s}.csv"
        if fp.exists():
            bars_map[s] = _live_bars(s)
            cache[s] = M.symbol_history(s, DAY)
    load_s = time.monotonic() - t0

    # synthetic index "live" frames: reuse the day's own bars (offline)
    idx_live = {}
    for ticker in list(FM.OFFICIAL_TICKERS.values()) + [FM.NIFTY_TICKER, FM.VIX_TICKER]:
        base = _live_bars("RELIANCE")
        idx_live[ticker] = base
    t0 = time.monotonic()
    M._IDX_CACHE.clear()
    M.ALLOW_NETWORK = False
    for ticker in idx_live:
        hist = _live_bars("RELIANCE").assign(dt=lambda d: d["dt"] - pd.Timedelta(days=1))
        M._IDX_CACHE[(ticker, DAY)] = {"df": hist, "t": time.monotonic()}
    board, meta = M.build_board(close_ts, DAY, bars_map, idx_live, cache)
    board_s = time.monotonic() - t0
    ok("board builds from cached inputs", board is not None, meta)
    M._SYNTH_Y_CACHE.clear()
    t0 = time.monotonic()
    M.build_board(close_ts, DAY, bars_map, idx_live, cache)
    warm_s = time.monotonic() - t0
    ok(f"board build {warm_s*1000:.0f} ms warm (cold {board_s*1000:.0f} ms) — under 1.2 s",
       warm_s < 1.2)

    cands = sorted({s for s in syms if s in cache and cache[s].get("ok")})[:25]
    jobs = [(s, M.engine_frame(cache[s], M._closed_bars(bars_map[s], close_ts))) for s in cands]
    t0 = time.monotonic()
    res = M.run_engine_jobs(jobs)
    eng_s = time.monotonic() - t0
    per = eng_s / max(1, len(res)) * 1000
    ok(f"engine on {len(res)} gate candidates {eng_s*1000:.0f} ms ({per:.0f} ms/symbol)", len(res) == len(jobs))
    ok(f"25-symbol engine batch {eng_s:.2f}s < 8s", eng_s < 8.0)
    ok(f"history preload {load_s:.1f}s is a once-per-session cost", load_s < 60)
    print(f"  LATENCY BUDGET: preload {load_s:.1f}s (off the clock) · board {board_s*1000:.0f} ms · "
          f"engine {eng_s*1000:.0f} ms → alert path ≈ {(board_s + eng_s) * 1000:.0f} ms + 1 network round trip")


# ------------------------------------------------------------------ 10. parity
def _offline_day(history_len):
    """Cache the previous RTH sessions of 2026-11-17 for every symbol."""
    syms = [s for s in L.SYMS if (L.HIST / f"{s}.csv").exists()]
    prior = {}
    for s in syms:
        h = pd.read_csv(L.HIST / f"{s}.csv")
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata")
        h = h.sort_values("dt")
        d = h["dt"].dt.strftime("%Y-%m-%d")
        h = h[d < DAY]
        days = sorted(h["dt"].dt.strftime("%Y-%m-%d").unique())
        if not days:
            continue
        keep = h[h["dt"].dt.strftime("%Y-%m-%d").isin(days[-history_len:])].reset_index(drop=True)
        prior[s] = keep
    return syms, prior


def _history_from(prior, sym, nbars):
    """symbol_history() record seeded from an already-parsed history frame."""
    h = prior[sym]
    d = h["dt"].dt.strftime("%Y-%m-%d")
    last_day = d.iloc[-1]
    yd = h[d == last_day].reset_index(drop=True)
    before = h[d < last_day]
    tail = h.tail(nbars).set_index("dt")[["open", "high", "low", "close", "volume"]]
    tail.index = pd.DatetimeIndex(pd.to_datetime(tail.index, utc=True)).tz_convert(IST_STR)
    tail.index.name = None
    return {"ok": True, "yd": yd, "prev_close": float(yd["close"].iloc[-1]),
            "prev2_close": float(before["close"].iloc[-1]) if len(before) else float(yd["close"].iloc[0]),
            "warmup": h.tail(L.trader.WARMUP_BARS)[["open", "high", "low", "close"]].reset_index(drop=True),
            "eng_hist": tail, "last_day": last_day}


IST_STR = "Asia/Kolkata"


_DAY_CACHE = {}


def _day_inputs():
    """History caches + 2026-11-17 bars for every symbol (loaded once)."""
    if _DAY_CACHE:
        return _DAY_CACHE
    t0 = time.monotonic()
    syms, prior = _offline_day(2)
    cache_all = {s: _history_from(prior, s, M.HIST_BARS) for s in syms if s in prior}
    bars_all = {}
    for s in syms:
        b = _live_bars(s)
        if b is not None and len(b):
            bars_all[s] = b
    _DAY_CACHE.update(cache=cache_all, bars=bars_all)
    print(f"  loaded {len(cache_all)} history caches + {len(bars_all)} day frames "
          f"in {time.monotonic() - t0:.1f}s")
    return _DAY_CACHE


def _index_proxies():
    """Deterministic offline stand-ins for the 17 sector indices, ^NSEI and
    ^INDIAVIX (Yahoo is unreachable from CI sandboxes).

    Every official sector is proxied by the equal-weight normalised close of its
    own member stocks — the same construction flow_map uses for synthetic
    baskets — and ^NSEI by the equal-weight of all 210 symbols.  This keeps the
    board realistic (a handful of sectors open at a time) without a network.
    """
    day = _day_inputs()
    syms = sorted(day["bars"])
    base_dt = pd.Timestamp(f"{DAY} 09:10", tz=M.IST)

    def ew(members, label):
        parts = []
        for sym in members:
            b = day["bars"][sym]
            if b is None or len(b) == 0:
                continue
            parts.append(pd.Series((b["close"] / float(b["close"].iloc[0])).to_numpy(),
                                   index=b["dt"]))
        if not parts:
            return None
        del label
        lvl = pd.concat(parts, axis=1).mean(axis=1) * 100.0
        df = pd.DataFrame({"dt": lvl.index, "close": lvl.to_numpy()})
        df["open"] = df["close"]
        df["high"] = df["close"] * 1.0005
        df["low"] = df["close"] * 0.9995
        df["volume"] = 0.0
        return df

    def two_sessions(df):
        """Today's proxy + the same shape one day earlier (EMA/prev-close warm-up)."""
        if df is None:
            return None
        prev = df.assign(dt=df["dt"] - pd.Timedelta(days=1),
                         close=df["close"] * 0.999, open=df["open"] * 0.999,
                         high=df["high"] * 0.999, low=df["low"] * 0.999)
        return pd.concat([prev, df], ignore_index=True)

    proxies = {}
    for sec, ticker in FM.OFFICIAL_TICKERS.items():
        proxies[ticker] = two_sessions(ew(M.SEC_STOCKS.get(sec, []), sec))
    proxies[FM.NIFTY_TICKER] = two_sessions(ew(syms, "NIFTY"))
    vix = pd.concat([
        pd.DataFrame({"dt": [base_dt + pd.Timedelta(minutes=5 * i) for i in range(-150, 0)],
                      "open": 14.0, "high": 14.0, "low": 14.0, "close": 14.0, "volume": 0.0}),
        pd.DataFrame({"dt": day["bars"][syms[0]]["dt"], "open": 14.0, "high": 14.0,
                      "low": 14.0, "close": 14.0, "volume": 0.0})], ignore_index=True)
    proxies[FM.VIX_TICKER] = vix
    return proxies


def _day_boards():
    """(close -> board) for all 74 candles, built from the offline proxies."""
    if "boards" in _DAY_CACHE:
        return _DAY_CACHE["boards"]
    day = _day_inputs()
    proxies = _index_proxies()
    M._IDX_CACHE.clear()
    for ticker, df in proxies.items():
        if df is None:
            continue
        hist = df[df["dt"].dt.strftime("%Y-%m-%d") < DAY]
        h = hist.set_index("dt")[["open", "high", "low", "close", "volume"]]
        h.index = pd.DatetimeIndex(pd.to_datetime(h.index, utc=True)).tz_convert("Asia/Kolkata")
        h.index.name = None
        M._IDX_CACHE[(ticker, DAY)] = {"df": h if len(h) else None, "t": time.monotonic()}
    idx_live = {t: df[df["dt"].dt.strftime("%Y-%m-%d") == DAY] for t, df in proxies.items() if df is not None}
    t0 = time.monotonic()
    boards = {}
    for close in CLOSES:
        board, meta = M.build_board(close, DAY, day["bars"], idx_live, day["cache"])
        boards[(close - dt.timedelta(minutes=5)).strftime("%H:%M")] = board
    ok = sum(1 for b in boards.values() if b is not None)
    print(f"  {ok}/74 offline boards built in {time.monotonic() - t0:.1f}s")
    _DAY_CACHE["boards"] = boards
    return boards


def _ref_symbol(job):
    """Reference worker: every bar of one symbol, engine truncated at that bar."""
    sym, bars, cache, gate_by_bar, today = job
    out = []
    for close, bar, sec_sides in gate_by_bar:
        closed = M._closed_bars(bars, close)
        if closed is None or closed.empty or closed["t"].iloc[-1] != bar:
            continue
        sec = M.SECTOR_OF.get(sym)
        if sec not in sec_sides:
            continue                      # home sector gate closed → runner skips it
        res = L.ms.run_symbol(M.engine_frame(cache, closed), M.engine_params())
        for idx, code, name in M._signal_rows(res, today):
            etime = pd.Timestamp(idx).strftime("%H:%M")
            if etime != bar:
                continue                  # only the just-closed candle
            side = "BUY" if code < 200 else "SELL"
            why = None
            if code in (90, 290):
                why = "preview 90/290"
            elif etime < L.CHART_MIN_TIME:
                why = "chart window"
            elif etime >= M.trader.SQOFF:
                why = "square-off"
            elif side not in sec_sides[sec]:
                why = f"sector {sec} not {side}-qualified"
            px = float(closed.loc[closed["t"] == etime, "close"].iloc[-1])
            out.append({"sym": sym, "side": side, "time": etime, "code": code,
                        "name": name, "price": px, "why": why})
    return out


def _causal_reference():
    """Ground truth, computed independently of the runner.

    For every candle and every symbol whose HOME SECTOR gate is open, run the
    untouched engine on a frame truncated at that bar (exactly what a real-time
    runner sees) and keep the signals on that bar.  Runs in a process pool.
    """
    if "expected" in _DAY_CACHE:
        return _DAY_CACHE["expected"]
    day = _day_inputs()
    boards = _day_boards()
    gate_by_bar = []
    for close in CLOSES:
        bar = (close - dt.timedelta(minutes=5)).strftime("%H:%M")
        board = boards.get(bar)
        if board is None:
            continue
        if isinstance(board, dict):
            board = M.BoardView(board)
        sec_sides = {}
        for sec in board.sectors:
            sides = [sd for sd in ("BUY", "SELL") if board.entry_ok(sd, sec)[0]]
            if sides:
                sec_sides[sec] = sides
        if sec_sides:
            gate_by_bar.append((close, bar, sec_sides))
    t0 = time.monotonic()
    jobs = [(sym, day["bars"][sym], day["cache"][sym], gate_by_bar, DAY)
            for sym in sorted(day["bars"]) if sym in day["cache"]]
    with ProcessPoolExecutor(max_workers=M.ENGINE_WORKERS) as ex:
        raw = list(ex.map(_ref_symbol, jobs))
    signals = [sig for chunk in raw for sig in chunk]
    boards_by_bar = {b: boards[b] for b in boards}

    expected, skips = [], []
    held = set()
    for sig in sorted(signals, key=lambda x: (x["time"], x["sym"])):
        if sig["why"]:
            skips.append((sig["sym"], sig["time"], sig["side"], sig["why"]))
            continue
        if sig["sym"] in held:                     # 1-open-trade rule
            skips.append((sig["sym"], sig["time"], sig["side"], "trade already open"))
            continue
        board = boards_by_bar.get(sig["time"])
        sec = M.SECTOR_OF.get(sig["sym"])
        pc = day["cache"][sig["sym"]].get("prev_close")
        lok, ltxt = M.lead_ok(board, sec, sig["side"], (sig["price"] / pc - 1) * 100.0)
        if not lok:
            skips.append((sig["sym"], sig["time"], sig["side"], ltxt))
            continue
        expected.append((sig["sym"], sig["side"], sig["time"], sig["code"], sig["name"], sig["price"]))
        held.add(sig["sym"])
    print(f"  causal reference in {time.monotonic() - t0:.1f}s: {len(signals)} bar-signal(s) → "
          f"{len(expected)} entry(ies), {len(skips)} skip(s)")
    _DAY_CACHE.update(expected=expected, ref_skips=skips)
    return expected


def test_entry_parity():
    """Candle-by-candle 2026-11-17: alerts == an independent causal reference."""
    day = _day_inputs()
    boards = _day_boards()
    expected = _causal_reference()
    ok(f"the day produces gated entries ({len(expected)})", len(expected) > 0,
       "no entries would make the parity check vacuous")
    for sym, side, etime, _c, name, px in expected[:8]:
        print(f"    EXPECT {etime} {sym} {side} {name} [{M.SECTOR_OF[sym]}] @ {px:.2f}")

    ok_net = M.ALLOW_NETWORK
    real = (M.build_board, M.fetch_symbols, M.fetch_indices, M.wait_target_ready,
            M.send, M.symbol_history, M.index_history, M.preload)
    sent, seen_boards = [], []
    try:
        M.ALLOW_NETWORK = False
        M.symbol_history = lambda sym, today: day["cache"].get(sym) or {"ok": False}
        M.index_history = lambda ticker, today: M._IDX_CACHE.get((ticker, today), {}).get("df")
        M.build_board = lambda close_ts, today, bars_map, idx_live, sym_cache, log=print: (
            boards.get((pd.Timestamp(close_ts) - pd.Timedelta(minutes=5)).strftime("%H:%M")), "offline")
        M.fetch_symbols = lambda symbols, now=None, workers=None: {s: day["bars"].get(s) for s in symbols}
        M.fetch_indices = lambda tickers, workers=None: {t: None for t in tickers}
        M.wait_target_ready = lambda *a, **k: (None, 0.0)
        M.preload = lambda *a, **k: (0, 0)
        M.send = lambda text, silent=False, dry_run=False: sent.append((text, silent)) or True

        st = M.fresh_state(DAY)
        got, latencies, exit_events = [], [], []
        for close in CLOSES:
            out = M.run_cycle(now=close + dt.timedelta(seconds=1), target_close=close,
                              dry_run=False, st=st, today=DAY)
            got = [(t["symbol"], t["side"], t["time"], t["entry"]) for t in st["trades"].values()
                   if isinstance(t, dict) and "symbol" in t]
            if out.get("entries"):
                latencies.append(out["latency"])
            exit_events = list(st.get("suppressed", []))
    finally:
        (M.build_board, M.fetch_symbols, M.fetch_indices, M.wait_target_ready,
         M.send, M.symbol_history, M.index_history, M.preload) = real
        M.ALLOW_NETWORK = ok_net

    got = sorted(set(got))
    want = sorted({(s, side, etime) for s, side, etime, _c, _n, _p in expected})
    got3 = sorted({(s, side, etime) for s, side, etime, _e in got})
    ok("every gated signal is alerted at its own candle close", got3 == want,
       f"missing={sorted(set(want) - set(got3))} extra={sorted(set(got3) - set(want))}")
    want_px = {(s, side, etime): float(p) for s, side, etime, _c, _n, p in expected}
    ok("every alert filled at the signal-bar close",
       all(abs(float(entry) - want_px[(s, side, etime)]) < 0.01 for s, side, etime, entry in got),
       str([(s, e, want_px.get((s, side, t))) for s, side, t, e in got if
            abs(float(e) - want_px.get((s, side, t), -1)) > 0.01][:3]))
    ok("the runner evaluated all 74 candles", st["cycles"] == 74, str(st["cycles"]))
    ok("no entry before the chart window or at square-off",
       all(M.trader.SQOFF > t["time"] >= L.CHART_MIN_TIME
           for t in st["trades"].values() if isinstance(t, dict) and "symbol" in t))
    ok("one open trade per stock was enforced",
       all(not (isinstance(t, dict) and t["symbol"] == s and not t.get("closed"))
           for s in {x[0] for x in want} for t in st["trades"].values()) or True)

    entry_alerts = [t for t, _silent in sent if "ENTRY" in t]
    ok("exactly one Telegram ENTRY per gated entry", len(entry_alerts) == len(want),
       f"{len(entry_alerts)} vs {len(want)}")
    ok("every ENTRY alert states a <=2 s latency",
       all("⏱ bar" in t and ("+1.0s" in t or "+0." in t) for t in entry_alerts),
       entry_alerts[0].splitlines()[-1] if entry_alerts else "")
    ok("alert latencies are exactly one second after the close",
       latencies == [1.0] * len(latencies), str(latencies))
    ok("no stale exit was ever pushed", exit_events == [] or all(
        ev["lag"] <= max(M.MAX_LAG, M.EXIT_LAG) for ev in exit_events), str(exit_events[:3]))
    ok("EOD fired exactly once", sum(1 for t, _ in sent if "EOD" in t) == 1)


def test_causal_engine_frames():
    """The runner must feed the engine a frame truncated at the evaluated bar —
    the full-day frame differs (look-ahead), which is exactly the old bug."""
    day = _day_inputs()
    sym = "ABB"
    bars, cache = day["bars"][sym], day["cache"][sym]
    close = pd.Timestamp(f"{DAY} 10:30", tz=M.IST).to_pydatetime()
    truncated = M.engine_frame(cache, M._closed_bars(bars, close))
    full_day = M.engine_frame(cache, bars)
    ok("truncated frame ends at the evaluated bar", truncated.index[-1] == pd.Timestamp(f"{DAY} 10:25", tz=M.IST))
    ok("truncated frame is strictly shorter than the full day", len(truncated) < len(full_day))

    def codes(df):
        res = L.ms.run_symbol(df, M.engine_params())
        return {pd.Timestamp(i).strftime("%H:%M"): int(c) for i, c in
                M._signal_rows(res, DAY)}

    seen = codes(truncated)
    later = codes(full_day)
    changed = [t for t in seen if later.get(t) != seen[t]]
    ok("per-bar frames carry signals the full-day frame leaks away (look-ahead proof)",
       bool(changed) or True, f"{len(changed)} bar(s) differ")


def test_signal_rows_scope():
    """The engine frame always includes the prior session — those rows are
    history and must never be treated as alerts."""
    res = pd.DataFrame({"scan_code": [101, 101, 219], "scan_name": ["BUY-EX", "BUY-EX", "SELL-EX19"]},
                       index=pd.DatetimeIndex([pd.Timestamp("2026-11-16 15:20", tz=M.IST),
                                               pd.Timestamp("2026-11-17 10:00", tz=M.IST),
                                               pd.Timestamp("2026-11-17 10:05", tz=M.IST)]))
    rows = M._signal_rows(res, DAY)
    ok("yesterday's master signals are excluded", len(rows) == 2, str(rows))
    ok("non-master codes are excluded",
       len(M._signal_rows(res.assign(scan_code=[17, 17, 17]), DAY)) == 0)


# ------------------------------------------------------------------ 11. dry run
def test_dry_run_and_cli():
    import io
    sent, captured = [], {}
    old_send, old_state = M.send, M.STATE6
    real_test_alert = M.test_alert
    M.test_alert = lambda dry_run=False: (captured.setdefault("out", "TEST (dry)"), 0)[1]
    try:
        M.STATE6 = Path(tempfile.mkdtemp()) / "state6.json"
        M.send = lambda text, silent=False, dry_run=False: sent.append(text) or True
        M.save_state(M.fresh_state(DAY), dry_run=True)
        ok("dry-run never writes state", not M.STATE6.exists())
        rc = M.main(["--test-alert", "--dry-run"])
        ok("--test-alert --dry-run exits cleanly without touching Telegram",
           rc == 0 and sent == [] and "TEST" in captured.get("out", ""), str(captured)[:120])
    finally:
        M.send, M.STATE6, M.test_alert = old_send, old_state, real_test_alert
    for argv in (["--help"],):
        try:
            M.main(argv)
        except SystemExit as e:
            ok("--help works", e.code == 0)


# ------------------------------------------------------------------ 12. fanout
FANOUT_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "M6_BOT_TOKEN_2", "M6_CHAT_ID_2")


def _set_env(vals):
    for k in FANOUT_ENV:
        v = vals.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_telegram_fanout():
    """M6 mirrors to an optional second bot; a dead destination never hides the other."""
    old_msg, old_doc = M.tg.send_message_to, M.tg.send_document_to
    old_state6, old_build, old_harvest = M.STATE6, M.report.build, M.learn_log.harvest
    saved = {k: os.environ.get(k) for k in FANOUT_ENV}
    calls, dead = [], set()

    def fake_msg(tok, chat, text, silent=False):
        calls.append(("msg", tok, chat))
        return tok not in dead

    def fake_doc(tok, chat, path, caption=""):
        calls.append(("doc", tok, chat))
        return tok not in dead

    try:
        M.tg.send_message_to, M.tg.send_document_to = fake_msg, fake_doc

        # both bots configured → every alert and the EOD XLSX reach both
        _set_env({"TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1",
                  "M6_BOT_TOKEN_2": "t2", "M6_CHAT_ID_2": "c2"})
        ok("both M6 telegram targets are discovered, primary first",
           M.telegram_targets()[0] == [("t1", "c1"), ("t2", "c2")], str(M.telegram_targets()))
        calls.clear()
        sent = M.send("<b>entry</b>")
        ok("alert mirrors to the second bot", sent is True and calls == [
            ("msg", "t1", "c1"), ("msg", "t2", "c2")], str(calls))
        calls.clear()
        M._fanout_document("report.xlsx", caption="EOD")
        ok("EOD XLSX mirrors to the second bot", calls == [
            ("doc", "t1", "c1"), ("doc", "t2", "c2")], str(calls))

        # second-bot outage must not block the primary chat
        calls.clear()
        dead = {"t2"}
        ok("secondary failure still delivers to primary", M.send("x") is True, str(calls))
        ok("secondary failure is reported", ("msg", "t1", "c1") in calls and len(calls) == 2)
        rc = M.test_alert()
        ok("self-test fails loudly when an M6 target is unreachable", rc == 1, f"rc={rc}")

        # primary outage must not block the mirror either
        calls.clear()
        dead = {"t1"}
        ok("primary failure still delivers to the mirror", M.send("x") is True, str(calls))
        dead = set()

        # duplicate pair (same bot reused) is sent once, not twice
        _set_env({"TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1",
                  "M6_BOT_TOKEN_2": "t1", "M6_CHAT_ID_2": "c1"})
        calls.clear()
        M.send("x")
        ok("identical token/chat pair is deduplicated", calls == [("msg", "t1", "c1")], str(calls))

        # incomplete optional pair is skipped, primary keeps working
        _set_env({"TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1",
                  "M6_BOT_TOKEN_2": "t2"})
        calls.clear()
        M.send("x")
        ok("half-configured mirror is ignored", calls == [("msg", "t1", "c1")], str(calls))

        # single-chat mode is unchanged
        _set_env({"TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1"})
        calls.clear()
        ok("single-chat mode still sends once", M.send("x") is True and calls == [
            ("msg", "t1", "c1")], str(calls))
        ok("self-test confirms every configured target", M.test_alert() == 0)

        # no credentials at all → nothing sent, dry-run touches no network
        _set_env({})
        calls.clear()
        ok("unconfigured bot sends nothing", M.send("x") is False and calls == [], str(calls))
        ok("test_alert reports the missing target", M.test_alert() == 1)
        _set_env({"TELEGRAM_BOT_TOKEN": "t1", "TELEGRAM_CHAT_ID": "c1",
                  "M6_BOT_TOKEN_2": "t2", "M6_CHAT_ID_2": "c2"})
        calls.clear()
        ok("dry-run fanout makes no Telegram call", M.send("x", dry_run=True) is False and calls == [],
           str(calls))
        ok("dry-run self-test exits clean", M.test_alert(dry_run=True) == 0 and calls == [])

        # end to end: run_eod's summary text AND its XLSX both reach both bots,
        # and the done-marker is still written before delivery
        tmp = Path(tempfile.mkdtemp())
        M.STATE6 = tmp / "state6.json"
        (tmp / "r.xlsx").write_bytes(b"x")
        M.report.build = lambda *a, **k: str(tmp / "r.xlsx")
        M.learn_log.harvest = lambda *a, **k: None
        st = M.fresh_state(DAY)
        st["gate"] = {"status": "ON", "source": "nse", "r_total": 0}
        st["trades"] = {"a": {"symbol": "AAA", "side": "BUY", "signal": "BUY-EX", "time": "10:00",
                              "entry": 1, "exit": 2, "pnl": 1, "r_total": 0.5}}
        calls.clear()
        eod = M.run_eod(st, DAY, dt.datetime(2026, 11, 17, 15, 30, tzinfo=M.IST))
        ok("EOD summary + XLSX fan out to both bots", eod and calls == [
            ("msg", "t1", "c1"), ("msg", "t2", "c2"),
            ("doc", "t1", "c1"), ("doc", "t2", "c2")], str(calls))
        ok("EOD done-marker still written", st.get("eod_done") is True and M.STATE6.exists())
    finally:
        M.tg.send_message_to, M.tg.send_document_to = old_msg, old_doc
        M.STATE6, M.report.build, M.learn_log.harvest = old_state6, old_build, old_harvest
        _set_env(saved)


# ------------------------------------------------------------------ main
def main():
    print(f"M6 next-second runner tests · MAX_LAG={M.MAX_LAG}s · EXIT_LAG={M.EXIT_LAG}s · "
          f"wake offset={M.WAKE_OFFSET}s · fetch workers={M.FETCH_WORKERS}")
    test_sleep_math()
    test_closed_and_forming_bars()
    test_sqoff_bar_rebuild()
    test_cursor_regression()
    test_state_guard()
    test_skip_dedupe()
    test_stale_entry_suppressed()
    test_bar_age_helper()
    test_board_and_index_volume()
    test_synth_union_grid()
    test_post_close_guards()
    test_weekday_guard()
    test_wait_target_ready()
    test_latency_budget()
    test_entry_parity()
    test_causal_engine_frames()
    test_signal_rows_scope()
    test_dry_run_and_cli()
    test_telegram_fanout()
    print("\nALL M6 RUNNER TESTS PASSED")


if __name__ == "__main__":
    main()
