#!/usr/bin/env python3
"""MODEL 6 — SECTOR-FLOW GATE paper trader — NEXT-SECOND alert runner.

M6 alerts must land on the FIRST SECOND after a 5-minute candle closes
(09:20:01 … 15:25:01 IST), never minutes later and never after the bell.
The previous runner broke that promise in four independent ways — all fixed here:

1. SCHEDULING.  ``--loop N`` slept an unaligned ``sleep(240)`` so two of every
   three candle closes were missed; the GitHub ``schedule`` cron fires 30–120
   minutes late (measured 2026-09/10: ``10:55–12:59Z`` = 16:25–18:29 IST).
   Now every cycle is pinned to ``candle_close + wake_offset`` (default +1.0 s)
   with a keep-alive warm-up 5 s before the close, and the workflow is driven
   by the real-time M15 completion event (cron-job.org proves sub-second
   triggers) plus a self-dispatch shift handoff.
2. COLD START.  A cycle re-read 210 history CSVs (≈105 ms each), re-fetched 210
   stocks + 19 indices serially behind artificial ``sleep(0.15/0.25/1.0)``
   calls and re-ran the 2 000-line engine 210 times → 3–6 min/cycle (99 min on
   a 75-bar catch-up).  Now history + prior-day index context are preloaded
   once (0 ms disk I/O at candle close), the 30-sector board is built from 17
   indices + 64 basket members in parallel (~1 s), and the untouched engine
   runs only on the handful of stocks whose sector gate is actually open.
3. STALE ALERTS.  There was no ``date == today`` filter (holiday replay), no
   bar-age guard and a ``known > n_today → 0`` cursor reset that replayed the
   whole day whenever Yahoo dropped a bar.  Every entry now needs: today's
   date, a CLOSED bar, ``0 <= lag <= M6_MAX_ALERT_LAG_SECONDS`` (120 s) and a
   monotonic per-symbol ``last_bar`` cursor.  Late events are recorded in
   state (audit) and never sent.
4. DOUBLE WORK.  Open trades, the sector board and the entry scan each needed
   the same bars; they now share one parallel fetch per candle.

Trading rules are unchanged (24-Jul-2026 spec: Q0=A Q1=A Q2=B Q3=B Q4=A):
master signal + home sector (fno_sector_map.csv — 17 official NSE sector
indices + 13 synthetic equal-weight baskets) side-coupled QUALIFIED, top-2 of
that side's board, persistent top-3, 12-bar timed hold, stock must lead its own
sector; entries 09:26+ only; 90/290 previews blocked; ₹50k/₹900 sizing, structure
SL, no targets, +1R structure trail, 15:20 square-off; 🅼6 alerts; ledger
state6.json.

Usage
  python -u m6_runner.py --session                    # aligned session loop (CI)
  python -u m6_runner.py --session --max-runtime-min 324 --git-sync
  python -u m6_runner.py --forever                    # always-on VM mode
  python -u m6_runner.py --loop 3                     # 3 aligned candles
  python -u m6_runner.py --once                       # one cycle right now
  python -u m6_runner.py --wait-for-bar               # next candle only
  python -u m6_runner.py --test-alert                 # Telegram self-test
  python -u m6_runner.py --session --dry-run          # print, never send/write
"""
from __future__ import annotations

import argparse
import atexit
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

import live_runner as L                  # engine, universe, engine_frame
import learn_log
import feeds, trader, report, flow_map as FM
import telegram_bot as tg

# --------------------------------------------------------------- configuration
ROOT = L.ROOT
STATE6 = ROOT / "state6.json"
SECTOR_OF = dict(pd.read_csv(ROOT / "fno_sector_map.csv").values)
OFFICIAL = set(FM.OFFICIAL_TICKERS)

MEMBERS: dict[str, list[str]] = {}
SEC_STOCKS: dict[str, list[str]] = {}
for _sym, _sec in SECTOR_OF.items():
    SEC_STOCKS.setdefault(_sec, []).append(_sym)
    if _sec not in OFFICIAL:
        MEMBERS.setdefault(_sec, []).append(_sym)
for _sec in SEC_STOCKS:
    SEC_STOCKS[_sec].sort()
for _sec in MEMBERS:
    MEMBERS[_sec].sort()

# 25-Jul-2026 user pick (option A): timed stable-flow hold 12×5m + beats-own-sector
# leader filter (unchanged by this rewrite).
FM.set_hold("timed", 12)

IST = L.IST
CANDLE_MIN = 5
SESSION_OPEN = dt.time(9, 15)
FIRST_CLOSE = dt.time(9, 20)
LAST_CLOSE = dt.time(15, 25)
MARKET_CLOSE = dt.time(15, 30)


def _env_int(name, default):
    try:
        return int(float(os.environ.get(name, default)))
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


WAKE_OFFSET = _env_float("M6_WAKE_OFFSET_SECONDS", 1.0)      # land at close + 1.0 s
MAX_LAG = _env_int("M6_MAX_ALERT_LAG_SECONDS", 120)          # entry freshness budget
EXIT_LAG = _env_int("M6_MAX_EXIT_LAG_SECONDS", 360)          # exits may be a bit later
FETCH_WORKERS = max(1, _env_int("M6_FETCH_WORKERS", 16))
ENGINE_WORKERS = max(1, _env_int("M6_ENGINE_WORKERS", min(4, os.cpu_count() or 1)))
HIST_BARS = max(300, _env_int("M6_HIST_BARS", 1500))
MIN_SECTORS = _env_int("M6_MIN_SECTORS", 20)                 # board strictness
KEEPALIVE_SEC = _env_float("M6_KEEPALIVE_SECONDS", 5.0)


# --------------------------------------------------------------- time helpers
def now_ist():
    return L.now_ist()


def session_closes(day, first=FIRST_CLOSE, last=LAST_CLOSE):
    """Every 5-minute candle close of one IST session, 09:20 … 15:25 (74)."""
    out = []
    t = dt.datetime.combine(day, first, tzinfo=IST)
    end = dt.datetime.combine(day, last, tzinfo=IST)
    while t <= end:
        out.append(t)
        t += dt.timedelta(minutes=CANDLE_MIN)
    return out


def next_wake_time(now, offset=WAKE_OFFSET, allow_rollover=False):
    """(wake, candle_close) for the next scheduled alert slot, or (None, None).

    wake = candle_close + offset, so the runner lands at 09:20:01 … 15:25:01.
    With allow_rollover (--forever) the next weekday's first slot is returned.
    """
    if offset is None:
        offset = WAKE_OFFSET
    for close in session_closes(now.date()):
        wake = close + dt.timedelta(seconds=float(offset))
        if wake > now:
            return wake, close
    if not allow_rollover:
        return None, None
    day = now.date() + dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    close = session_closes(day)[0]
    return close + dt.timedelta(seconds=float(offset)), close


def _sleep_until(target, deadline=None, warm=True, now_fn=now_ist, sleep_fn=time.sleep):
    """Sleep until `target` in bounded chunks; False if `deadline` passed first.

    Long waits are chunked (20 s → 1 s → exact) so the final wake lands within a
    few ms of the candle close, and the last 5 s are used to warm the Yahoo
    cookie/crumb + keep-alive socket so the first fetch at close+1 s is hot.
    """
    if warm:
        pre = target - dt.timedelta(seconds=KEEPALIVE_SEC)
        if (pre - now_fn()).total_seconds() > 2.0:
            if not _sleep_until(pre, deadline=deadline, warm=False, now_fn=now_fn, sleep_fn=sleep_fn):
                return False
            try:
                feeds.warm_yahoo_session()
            except Exception:
                pass
    while True:
        rem = (target - now_fn()).total_seconds()
        if rem <= 0:
            return True
        if deadline is not None and now_fn() >= deadline:
            return False
        sleep_fn(rem if rem < 1.0 else min(rem, 20.0) if rem > 60 else min(rem, 1.0))


def _bar_close_ts(hhmm, day):
    """Trading timestamp when a bar labelled HH:MM closes (label + 5 minutes)."""
    t = pd.Timestamp(f"{day} {hhmm}", tz=IST) + pd.Timedelta(minutes=CANDLE_MIN)
    return t.to_pydatetime()


def _fmt(ts):
    return ts.strftime("%Y-%m-%d %H:%M:%S%z")


# --------------------------------------------------------------- state handling
class StateCorrupt(RuntimeError):
    """state6.json is unreadable — fail closed: never guess, never alert."""


def fresh_state(today):
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0, "live_cycles": 0, "skipped": [],
            "suppressed": [], "latency": []}


def load_state(today):
    if STATE6.exists():
        raw = STATE6.read_text()
        try:
            st = json.loads(raw)
        except Exception as e:
            raise StateCorrupt(f"{STATE6.name} is not valid JSON: {e}") from e
        if not isinstance(st, dict):
            raise StateCorrupt(f"{STATE6.name} is not a JSON object")
        if st.get("date") == today:
            for k, v in fresh_state(today).items():
                st.setdefault(k, v)
            return st
    return fresh_state(today)


def save_state(st, dry_run=False):
    """Atomic state write — a torn state6.json used to fail every later run."""
    if dry_run:
        return
    try:
        tmp = STATE6.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str))
        os.replace(tmp, STATE6)
    except Exception as e:
        print(f"  state save failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------- bar utilities
def _prep(df):
    """Normalize a feed frame: IST tz-aware, sorted, deduped, with 't' HH:MM."""
    if df is None or len(df) == 0:
        return None
    out = df.copy()
    if "dt" not in out.columns:
        return None
    out["dt"] = pd.to_datetime(out["dt"], utc=True).dt.tz_convert(IST)
    out = out.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
    for col in ("open", "high", "low", "close"):
        if col not in out.columns:
            return None
    if "volume" not in out.columns:
        out["volume"] = 0.0
    out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    out["t"] = out["dt"].dt.strftime("%H:%M")
    return out


def _today_only(bars, day):
    if bars is None or len(bars) == 0:
        return bars
    if not isinstance(day, str):
        day = day.strftime("%Y-%m-%d")
    return bars[bars["dt"].dt.strftime("%Y-%m-%d") == day].reset_index(drop=True)


def _closed_bars(bars, close_ts):
    """Strict closed-bar guard: only bars whose 5-minute window has finished."""
    if bars is None or len(bars) == 0:
        return bars
    ts = pd.Timestamp(close_ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize(IST)
    mask = (bars["dt"] + pd.Timedelta(minutes=CANDLE_MIN)) <= ts
    out = bars[mask].reset_index(drop=True)
    if len(out) and "t" not in out.columns:
        out = out.assign(t=out["dt"].dt.strftime("%H:%M"))
    return out


def _has_bar(bars, hhmm):
    return bars is not None and len(bars) > 0 and bool((bars["t"] == hhmm).any())


def _ensure_sqoff_bar(bars, day):
    """Guarantee a 15:20 row so trader.evaluate can square a position off.

    Yahoo omits the 15:20 bar on ~98% of NSE symbols (measured 2026-11-17: only
    4/210 had it; the rest jump 15:15 → 15:25).  The 15:25 bar's OPEN is the
    price at 15:20, i.e. exactly the square-off print, so synthesise the missing
    bar from it.  Without this, an open trade ends the day marked OPEN (pnl 0).
    """
    if bars is None or len(bars) == 0 or "t" not in bars.columns:
        return bars
    if (bars["t"] == trader.SQOFF).any():
        return bars
    src = bars[bars["t"] == "15:25"]
    if src.empty:
        return bars
    px = float(src["open"].iloc[0])
    row = {"dt": pd.Timestamp(f"{day} {trader.SQOFF}", tz=IST),
           "open": px, "high": px, "low": px, "close": px, "volume": 0.0,
           "t": trader.SQOFF}
    out = pd.concat([bars, pd.DataFrame([row])], ignore_index=True)
    return out.sort_values("dt").reset_index(drop=True)


def _new_bars(st, sym, closed):
    """Bar labels newer than the symbol's monotonic cursor.

    The old runner stored only ``nbars`` and reset it to 0 when the feed came
    back thinner than before — replaying 09:15… every cycle (stale alerts).
    ``last_bar`` is monotonic and never moves backwards; a legacy state without
    ``last_bar`` adopts the current bar and evaluates nothing (no replay).
    """
    cur = st.get("signals", {}).get(sym) or {}
    last = cur.get("last_bar")
    labels = closed["t"].tolist() if closed is not None and len(closed) else []
    if last is None:
        if int(cur.get("nbars") or 0) > 0:
            _advance_cursor(st, sym, closed)
            return []
        return labels
    return [t for t in labels if t > last]


def _advance_cursor(st, sym, closed):
    if closed is None or len(closed) == 0:
        return
    cur = st.setdefault("signals", {}).get(sym) or {}
    last = str(closed["t"].iloc[-1])
    prev = cur.get("last_bar")
    if prev and str(prev) > last:              # never move backwards
        last = str(prev)
    st["signals"][sym] = {"last_bar": last,
                          "nbars": max(len(closed), int(cur.get("nbars") or 0))}


def _add_skip(st, row):
    """Record a skipped signal once (per symbol/time/side/signal)."""
    key = (row.get("symbol"), row.get("time"), row.get("side"), row.get("signal"))
    for it in reversed(st.get("skipped", [])[-500:]):
        if (it.get("symbol"), it.get("time"), it.get("side"), it.get("signal")) == key:
            return False
    st.setdefault("skipped", []).append(row)
    return True


# --------------------------------------------------------------- history caches
_SYM_CACHE: dict[str, dict] = {}
_IDX_CACHE: dict[tuple, dict] = {}
_CACHE_LOCK = threading.Lock()
ALLOW_NETWORK = True          # tests flip this off to stay fully offline


def symbol_history(sym, today):
    """Cached prior-day context for one symbol: yesterday bars, prev closes,
    engine history tail and the trader warm-up tail.  Read once per session so a
    candle-close cycle does zero history disk I/O."""
    fp = L.HIST / f"{sym}.csv"
    try:
        mtime = fp.stat().st_mtime_ns if fp.exists() else None
    except OSError:
        mtime = None
    key = (today, mtime)
    with _CACHE_LOCK:
        hit = _SYM_CACHE.get(sym)
        if hit is not None and hit.get("_key") == key:
            return hit
    rec = {"_key": key, "ok": False}
    if mtime is not None:
        try:
            h = pd.read_csv(fp)
            h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert(IST)
            h = h.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
            h["volume"] = pd.to_numeric(h.get("volume", 0), errors="coerce").fillna(0.0)
            h = h[h["dt"].dt.strftime("%Y-%m-%d") < today].reset_index(drop=True)
            hist = h
            if len(hist):
                d = hist["dt"].dt.strftime("%Y-%m-%d").to_numpy()
                last_day = d[-1]
                yd = hist[d == last_day].reset_index(drop=True)
                before = hist[d < last_day]
                prev_close = float(yd["close"].iloc[-1])
                prev2_close = float(before["close"].iloc[-1]) if len(before) else prev_close
                tail = hist.tail(HIST_BARS).set_index("dt")[["open", "high", "low", "close", "volume"]]
                tail.index = pd.to_datetime(tail.index, utc=True).tz_convert(IST)
                tail.index.name = None
                rec.update(ok=True, yd=yd, prev_close=prev_close, prev2_close=prev2_close,
                           warmup=hist.tail(trader.WARMUP_BARS)[["open", "high", "low", "close"]]
                           .reset_index(drop=True),
                           eng_hist=tail, last_day=last_day)
        except Exception as e:
            print(f"  history {sym}: {type(e).__name__}: {e}")
    with _CACHE_LOCK:
        _SYM_CACHE[sym] = rec
    return rec


def index_history(ticker, today):
    """Cached prior-day 5m bars for one index ticker (board EMA warm-up)."""
    key = (ticker, today)
    with _CACHE_LOCK:
        hit = _IDX_CACHE.get(key)
    if hit is not None and (hit["df"] is not None or time.monotonic() - hit["t"] < 60):
        return hit["df"]
    df = None
    if not ALLOW_NETWORK:
        return None
    try:
        raw = FM.fetch_index_bars(ticker, "5d")
        bars = _prep(raw)
        if bars is not None and len(bars):
            bars = bars[bars["dt"].dt.strftime("%Y-%m-%d") < today].reset_index(drop=True)
            df = bars if len(bars) else None
    except Exception as e:
        print(f"  index history {ticker}: {type(e).__name__}: {e}")
    with _CACHE_LOCK:
        _IDX_CACHE[key] = {"df": df, "t": time.monotonic()}
    return df


def preload(today, symbols=None, tickers=None, log=print):
    """Warm every cache a candle-close cycle needs (run once before 09:20)."""
    symbols = list(symbols or L.SYMS)
    tickers = list(tickers or (list(FM.OFFICIAL_TICKERS.values()) + [FM.NIFTY_TICKER, FM.VIX_TICKER]))
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(symbols)))) as ex:
        list(ex.map(lambda s: symbol_history(s, today), symbols))
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tickers)))) as ex:
        list(ex.map(lambda t: index_history(t, today), tickers))
    ok = sum(1 for s in symbols if symbol_history(s, today).get("ok"))
    idx = sum(1 for t in tickers if index_history(t, today) is not None)
    log(f"M6 preload: {ok}/{len(symbols)} symbol histories · {idx}/{len(tickers)} index histories "
        f"· {time.monotonic() - t0:.1f}s")
    return ok, idx


def _stitch_index(ticker, today, live):
    """prior-day cached bars + today's live bars → one frame (c_full warm-up)."""
    hist = index_history(ticker, today)
    bars = _prep(live)
    if bars is not None:
        bars = bars[bars["dt"].dt.strftime("%Y-%m-%d") == today]
    if hist is None or len(hist) == 0:
        return bars if bars is not None and len(bars) else None
    if bars is None or len(bars) == 0:
        return hist
    out = pd.concat([hist, bars], ignore_index=True)
    return _prep(out)


# --------------------------------------------------------------- board
class BoardView:
    """flow_map.compute_board() returns a dict; this thin view exposes the same
    data as attributes so board consumers never depend on the container type."""

    def __init__(self, data):
        self._d = data or {}
        for key in ("sectors", "bull_q", "bear_q", "persist_bull", "persist_bear",
                    "net_tilt", "tilt_label", "bull_top2", "bear_top2", "hold_mode",
                    "held_bull", "held_bear", "hold_bull", "hold_bear",
                    "hold_bull_qual", "hold_bear_qual", "vix_mult"):
            setattr(self, key, self._d.get(key))
        self.sectors = self.sectors or {}
        self.bull_q = self.bull_q or []
        self.bear_q = self.bear_q or []
        self.persist_bull = self.persist_bull or []
        self.persist_bear = self.persist_bear or []
        self.bull_top2 = self.bull_top2 or []
        self.bear_top2 = self.bear_top2 or []
        self.held_bull = self.held_bull or []
        self.held_bear = self.held_bear or []
        self.net_tilt = self.net_tilt if self.net_tilt is not None else 0.0
        self.tilt_label = self.tilt_label or "NEUTRAL"

    def entry_ok(self, side, sector):
        return self._d["entry_ok"](side, sector)

    def get(self, key, default=None):
        return self._d.get(key, default)


def _idx_slice(df, today, close_ts):
    """Today's closed bars + prior-day context arrays for one index."""
    if df is None or len(df) == 0:
        return None
    df = _prep(df) if "dt" not in df.columns else df      # cached frames are indexed
    if df is None or len(df) == 0:
        return None
    d = df["dt"].dt.strftime("%Y-%m-%d")
    hist = df[d < today]
    t = _closed_bars(df[d == today], close_ts)
    if t is None or t.empty:
        return None
    orb = t.head(6)
    prev_close = float(hist["close"].iloc[-1]) if len(hist) else float(t["open"].iloc[0])
    return dict(o=t["open"].values, h=t["high"].values, l=t["low"].values, c=t["close"].values,
                c_full=pd.concat([hist["close"], t["close"]]).values,
                prev_close=prev_close, day_open=float(t["open"].iloc[0]),
                or_high=float(orb["high"].max()), or_low=float(orb["low"].min()),
                n=int(len(t)), last_t=str(t["t"].iloc[-1]), last_dt=t["dt"].iloc[-1])


def _smooth(arr):
    """forward/back fill NaNs inside a level series (union-grid gaps)."""
    return pd.Series(arr, dtype="float64").ffill().bfill().values


def _nanmean(parts):
    """Row-wise mean ignoring NaNs without the np.nanmean overhead/warnings."""
    m = np.vstack(parts)
    cnt = np.isfinite(m).sum(axis=0)
    tot = np.where(np.isfinite(m), m, 0.0).sum(axis=0)
    return np.divide(tot, cnt, out=np.full(m.shape[1], np.nan), where=cnt > 0)


_SYNTH_Y_CACHE: dict[tuple, dict] = {}


def _synth_baseline(sec, today, sym_cache):
    """Yesterday's equal-weight basket levels — constant all session, so it is
    built once and reused by every candle-close cycle (≈80 ms/sector saved)."""
    key = (sec, today)
    with _CACHE_LOCK:
        hit = _SYNTH_Y_CACHE.get(key)
    if hit is not None:
        return hit
    store = []
    for sym in MEMBERS.get(sec, []):
        c = sym_cache.get(sym) or symbol_history(sym, today)
        if not c.get("ok"):
            continue
        yd = c["yd"]
        frame = yd.assign(t=yd["dt"].dt.strftime("%H:%M")).set_index("t")[
            ["open", "high", "low", "close"]]
        store.append((frame, float(c["prev2_close"])))
    if not store:
        with _CACHE_LOCK:
            _SYNTH_Y_CACHE[key] = None
        return None
    grid = sorted(set().union(*[set(f.index) for f, _ in store]))
    out = {"grid": grid}
    for col in ("open", "high", "low", "close"):
        parts = [(f[col] / base - 1).reindex(pd.Index(grid)).to_numpy(dtype="float64")
                 for f, base in store]
        out[col] = 100.0 * (1.0 + _smooth(_nanmean(parts)))
    with _CACHE_LOCK:
        _SYNTH_Y_CACHE[key] = out
    return out


def _synth_input(sec, today, close_ts, bars_map, sym_cache):
    """Stitched yesterday+today equal-weight level series for a synthetic sector.

    The timestamp grid is the UNION of every member's bars.  The old version
    took member #0's grid, so one lagging member shifted every other member's
    series by a bar (wrong board at exactly the moments that matter).
    Yesterday's half is cached per session (see _synth_baseline).
    """
    base = _synth_baseline(sec, today, sym_cache)
    if base is None:
        return None
    store = []
    for sym in MEMBERS.get(sec, []):
        b = bars_map.get(sym)
        if b is None:
            continue
        t = _closed_bars(b, close_ts)
        if t is None or t.empty:
            continue
        c = sym_cache.get(sym) or symbol_history(sym, today)
        if not c.get("ok"):
            continue
        store.append((t.set_index("t")[["open", "high", "low", "close"]],
                      float(c["yd"]["close"].iloc[-1])))
    if not store:
        return None
    grid = sorted(set().union(*[set(f.index) for f, _ in store]))
    parts = {col: [(f[col] / px - 1).reindex(pd.Index(grid)).to_numpy(dtype="float64")
                   for f, px in store] for col in ("open", "high", "low", "close")}
    prev_close = float(base["close"][-1])
    tl = {col: prev_close * (1.0 + _smooth(_nanmean(parts[col])))
          for col in ("open", "high", "low", "close")}
    return dict(o=tl["open"], h=tl["high"], l=tl["low"], c=tl["close"],
                c_full=np.concatenate([base["close"], tl["close"]]),
                prev_close=prev_close, day_open=float(tl["open"][0]),
                or_high=float(np.nanmax(tl["high"][:6])), or_low=float(np.nanmin(tl["low"][:6])),
                n=int(len(tl["close"])), last_t=str(grid[-1]))


def lead_ok(board, sec, side, sd):
    """beats-own-sector leader test (0 margin): BUY needs stock day% >= sector day%,
    SELL needs stock day% <= sector day%. Waived only if sector metrics missing."""
    d = board.sectors.get(sec) if sec else None
    if d is None:
        return True, "no sector metrics — lead waived"
    secd = d["day_ret"]
    if side == "BUY" and sd < secd:
        return False, f"lags own sector (stock {sd:+.2f}% vs {sec} {secd:+.2f}%)"
    if side == "SELL" and sd > secd:
        return False, f"lags own sector (stock {sd:+.2f}% vs {sec} {secd:+.2f}%)"
    return True, f"LEAD {sd:+.2f}% vs {sec} {secd:+.2f}%"


def build_board(close_ts, today, bars_map, idx_live, sym_cache, log=print):
    """Exact 30-sector board as of `close_ts`. Returns (board, meta) / (None, why)."""
    secs = {}
    ok_off = fail_off = 0
    stale = []
    for sec, ticker in FM.OFFICIAL_TICKERS.items():
        stitched = _stitch_index(ticker, today, idx_live.get(ticker))
        s = _idx_slice(stitched, today, close_ts) if stitched is not None else None
        if s is None:
            fail_off += 1
            continue
        secs[sec] = s
        ok_off += 1
        if s["last_t"] != (pd.Timestamp(close_ts) - pd.Timedelta(minutes=CANDLE_MIN)).strftime("%H:%M"):
            stale.append(f"{sec}@{s['last_t']}")
    synth_ok = 0
    for sec in MEMBERS:
        s = _synth_input(sec, today, close_ts, bars_map, sym_cache)
        if s is not None:
            secs[sec] = s
            synth_ok += 1
    nif = _idx_slice(_stitch_index(FM.NIFTY_TICKER, today, idx_live.get(FM.NIFTY_TICKER)), today, close_ts)
    meta = f"official {ok_off}/17 · synth {synth_ok}/13"
    if stale:
        meta += f" · stale {','.join(stale[:4])}{'…' if len(stale) > 4 else ''}"
    if nif is None:
        return None, meta + " · nifty DOWN"
    if len(secs) < MIN_SECTORS:
        return None, meta + f" · only {len(secs)} sectors (<{MIN_SECTORS})"
    n_intra = (nif["c"] - nif["day_open"]) / nif["day_open"] * 100.0
    vix_df = _stitch_index(FM.VIX_TICKER, today, idx_live.get(FM.VIX_TICKER))
    vix = None
    if vix_df is not None:
        v = _idx_slice(vix_df, today, close_ts)
        vix = float(v["c"][-1]) if v is not None else None
    open_dt = dt.datetime.combine(close_ts.date(), SESSION_OPEN, tzinfo=IST)
    mins = int((pd.Timestamp(close_ts) - pd.Timestamp(open_dt)).total_seconds() // 60)
    board = FM.compute_board(secs, {"n_intra": n_intra}, vix, mins)
    board["_meta"] = meta
    board["_nifty_last"] = nif["last_t"]
    return BoardView(board), meta


# --------------------------------------------------------------- fetching
def _fetch_symbol(sym, now):
    try:
        b, _src = feeds.fetch_today(sym, L.SID.get(sym), now)
        return sym, _prep(b)
    except Exception as e:
        print(f"  feed {sym}: {type(e).__name__}: {e}")
        return sym, None


def fetch_symbols(symbols, now=None, workers=None):
    """Parallel today-bars fetch. Returns {sym: bars|None}."""
    symbols = [s for s in dict.fromkeys(symbols) if s]
    if not symbols:
        return {}
    now = now or now_ist()
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers or FETCH_WORKERS, len(symbols))) as ex:
        futs = [ex.submit(_fetch_symbol, s, now) for s in symbols]
        for fut in as_completed(futs):
            sym, b = fut.result()
            out[sym] = b
    return out


def fetch_indices(tickers, workers=None):
    """Parallel index fetch (Yahoo 1d slices through the shared session)."""
    tickers = [t for t in dict.fromkeys(tickers) if t]
    if not tickers:
        return {}

    def one(ticker):
        try:
            return ticker, FM.fetch_index_bars(ticker, "1d")
        except Exception as e:
            print(f"  index {ticker}: {type(e).__name__}: {e}")
            return ticker, None

    out = {}
    with ThreadPoolExecutor(max_workers=min(workers or FETCH_WORKERS, len(tickers))) as ex:
        for fut in as_completed([ex.submit(one, t) for t in tickers]):
            ticker, df = fut.result()
            out[ticker] = df
    return out


def wait_target_ready(target_bar, close_ts, max_lag=None, fetch=None, now_fn=now_ist, sleep_fn=time.sleep):
    """Poll ^NSEI until `target_bar` is published; fast first, then back off.

    0.5 s for the first 10 s, 1 s to 30 s, 2 s after that, hard stop at
    close + max_lag. Returns (bars, waited_seconds). Never raises.
    """
    max_lag = MAX_LAG if max_lag is None else max_lag
    fetch = fetch or (lambda: FM.fetch_index_bars(FM.NIFTY_TICKER, "1d"))
    deadline = pd.Timestamp(close_ts).to_pydatetime() + dt.timedelta(seconds=max_lag)
    t0 = now_fn()
    bars = None
    while True:
        try:
            bars = _prep(fetch())
        except Exception as e:
            print(f"  nifty wait: {type(e).__name__}: {e}")
            bars = None
        if _has_bar(_today_only(bars, close_ts.strftime("%Y-%m-%d")), target_bar):
            return bars, (now_fn() - t0).total_seconds()
        now = now_fn()
        if now >= deadline:
            return bars, (now - t0).total_seconds()
        el = (now - t0).total_seconds()
        step = 0.5 if el < 10 else 1.0 if el < 30 else 2.0
        sleep_fn(max(0.02, min(step, (deadline - now).total_seconds())))


# --------------------------------------------------------------- engine
_ENGINE_PARAMS_KW = dict(enable_buy_ex10=False, enable_buy_ex11=False)
_ENGINE_POOL = None
_ENGINE_LOCK = threading.Lock()


def engine_params():
    return L.ms.Params(**_ENGINE_PARAMS_KW)


def _engine_worker(job):
    sym, df = job
    return sym, L.ms.run_symbol(df, engine_params())


def _engine_executor():
    global _ENGINE_POOL
    if ENGINE_WORKERS <= 1:
        return None
    with _ENGINE_LOCK:
        if _ENGINE_POOL is None:
            _ENGINE_POOL = ProcessPoolExecutor(max_workers=ENGINE_WORKERS)
        return _ENGINE_POOL


def _shutdown_engine_pool():
    global _ENGINE_POOL
    with _ENGINE_LOCK:
        if _ENGINE_POOL is not None:
            try:
                _ENGINE_POOL.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _ENGINE_POOL = None


atexit.register(_shutdown_engine_pool)


def run_engine_jobs(jobs, on_result=None):
    """Run the untouched engine on [(sym, frame)] and call on_result as each
    result lands (so the first entry alert never waits for the whole batch)."""
    global _ENGINE_POOL
    out = {}
    if not jobs:
        return out
    ex = _engine_executor() if len(jobs) > 1 else None
    if ex is not None:
        try:
            futs = {ex.submit(_engine_worker, job): job[0] for job in jobs}
            for fut in as_completed(futs):
                sym, res = fut.result()
                out[sym] = res
                if on_result:
                    on_result(sym, res)
            return out
        except Exception as e:
            print(f"  engine pool failed ({type(e).__name__}: {e}) — sequential retry")
            with _ENGINE_LOCK:
                try:
                    _ENGINE_POOL.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass
                _ENGINE_POOL = None
    for sym, df in jobs:
        if sym in out:
            continue
        try:
            res = L.ms.run_symbol(df, engine_params())
        except Exception as e:
            print(f"  engine {sym}: {type(e).__name__}: {e}")
            continue
        out[sym] = res
        if on_result:
            on_result(sym, res)
    return out


def engine_frame(cache, today_closed):
    """history tail + today's closed bars → the exact frame live_runner builds."""
    t = today_closed.set_index("dt")[["open", "high", "low", "close", "volume"]]
    hist = cache.get("eng_hist") if cache else None
    df = t if hist is None or len(hist) == 0 else pd.concat([hist, t])
    df = df.sort_index()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(IST)
    df.index.name = None
    return df


# --------------------------------------------------------------- notifier
def send(text, silent=False, dry_run=False):
    if dry_run:
        print(f"[DRY] {text}")
        return False
    return tg.send_message(text, silent=silent)


def test_alert(dry_run=False):
    msg = ("🧪 🅼6 TEST — next-second runner connected. Alerts fire at candle "
           "close +1s (09:20:01 … 15:25:01 IST). No position was opened.")
    ok = send(msg, dry_run=dry_run)
    print(f"M6 test alert: {'sent' if ok else 'not confirmed'}")
    return 0 if (ok or dry_run) else 1


def git_sync(reason="", dry_run=False, next_close=None, timeout=25):
    """Push today's M6 files after alerts are out (never in the alert path).

    Skipped within 15 s of the next candle close so a slow rebase cannot delay
    an alert; the next cycle (or the workflow's commit step) picks it up.
    """
    if dry_run or os.environ.get("M6_GIT_SYNC") != "1":
        return False
    if next_close is not None:
        try:
            if (next_close - now_ist()).total_seconds() < 15:
                print("  git-sync deferred (next candle <15s away)")
                return False
        except Exception:
            pass
    files = ["state6.json"]
    files += sorted(p.name for p in ROOT.glob("paper_test_M6_*.xlsx"))

    def run(args):
        return subprocess.run(["git", "-C", str(ROOT)] + args, capture_output=True, text=True,
                              timeout=timeout)

    try:
        branch = run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        if branch != "main":
            print(f"  git-sync skipped (branch {branch or '?'} is not main)")
            return False
        run(["add", "--"] + files)
        if run(["diff", "--cached", "--quiet"]).returncode != 0:
            run(["commit", "-m", f"m6 {reason} {dt.datetime.utcnow():%Y-%m-%dT%H:%M:%SZ}"])
        pull = run(["pull", "--rebase", "-X", "ours", "origin", "main"])
        if pull.returncode != 0:
            print(f"  git-sync pull: {(pull.stderr or pull.stdout).strip()[-200:]}")
            run(["rebase", "--abort"])
        push = run(["push", "origin", "HEAD:main"])
        if push.returncode != 0:
            print(f"  git-sync push: {(push.stderr or push.stdout).strip()[-200:]}")
            return False
        return True
    except Exception as e:
        print(f"  git-sync {reason}: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------- trade mgmt
def manage_trades(st, today, close_ts, now, bars_map, sym_cache, dry_run=False, max_lag=None):
    """Deterministic re-evaluation of every open trade with the new bars.

    Fires TRAIL_ON / EXIT_SL / EXIT_EOD immediately when fresh.  A stale event
    (more than max(MAX_LAG, EXIT_LAG) seconds old — e.g. a runner that woke up
    after the bell) is recorded as suppressed and never sent.
    """
    max_lag = MAX_LAG if max_lag is None else max_lag
    budget = max(max_lag, EXIT_LAG)
    sent = 0
    for tkey in list(st.get("trades", {})):
        tr = st["trades"][tkey]
        if not isinstance(tr, dict) or "symbol" not in tr:
            continue
        sym = tkey.split("#")[0]
        bars = bars_map.get(sym)
        if bars is None:
            continue
        closed = _closed_bars(bars, close_ts)
        if closed is None or closed.empty:
            continue
        cache = sym_cache.get(sym) or {}
        try:
            new_tr = trader.evaluate(sym, tr["side"], tr["time"], float(tr["entry"]), tr["signal"],
                                     closed, warmup=cache.get("warmup"),
                                     sl_mode=tr.get("sl_mode", "structure"))
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")
            continue
        if "error" in new_tr:
            continue
        new_tr["sector"] = tr.get("sector")
        st["trades"][tkey] = new_tr
        for ev in new_tr.get("events", []):
            if ev["key"] == "ENTRY":
                continue
            key = f"{tkey}:{ev['key']}"
            if key in st["alerts"]:
                continue
            lag = (now - _bar_close_ts(ev["time"], today)).total_seconds()
            st["alerts"].append(key)
            if lag > budget:
                st.setdefault("suppressed", []).append(
                    {"trade": tkey, "event": ev["key"], "time": ev["time"], "lag": round(lag, 1)})
                print(f"  suppressed stale {key} ({lag:.0f}s old)")
                continue
            save_state(st, dry_run)        # registry saved BEFORE send (no duplicates)
            send("🅼6 · " + trader.fmt_alert(new_tr, ev["key"]), dry_run=dry_run)
            sent += 1
            print(f"  >>> M6 {ev['key']} {tkey} @ {ev['price']} ({lag:+.1f}s after bar close)")
    return sent


# --------------------------------------------------------------- entry scan
def _signal_rows(res, today=None):
    """(timestamp, code, name) for every master-signal row of an engine result.

    `today` restricts the scan to THIS session: the engine frame always carries
    the tail of the previous session, whose rows are history, not alerts.
    """
    if res is None or len(res) == 0 or "scan_code" not in res.columns:
        return []
    codes = pd.to_numeric(res["scan_code"], errors="coerce")
    hit = res[codes.notna() & codes.astype("Int64").isin(sorted(L.MASTER_CODES))]
    if today is not None and len(hit):
        hit = hit[[pd.Timestamp(i).tz_convert(IST).strftime("%Y-%m-%d") == today
                   if getattr(i, "tzinfo", None) is not None
                   else pd.Timestamp(i).strftime("%Y-%m-%d") == today for i in hit.index]]
    if len(hit) == 0:
        return []
    names = hit["scan_name"] if "scan_name" in hit.columns else pd.Series(index=hit.index, dtype=object)
    return [(idx, int(codes.loc[idx]), str(names.loc[idx] if pd.notna(names.loc[idx]) else int(codes.loc[idx])))
            for idx in hit.index]


def scan_entries(st, today, close_ts, now, board, bars_map, sym_cache, dry_run=False,
                 max_lag=None, fetch_missing=True):
    """Two-stage entry scan.

    Stage 1 (already done): board from 17 indices + 64 basket members.
    Stage 2 (here): only stocks whose HOME SECTOR GATE IS OPEN need today's
    bars; they are fetched in parallel, lead-filtered, and the untouched engine
    runs on the few that survive.  Alerts are sent the instant a signal is
    confirmed fresh.
    """
    max_lag = MAX_LAG if max_lag is None else max_lag
    stats = {"entries": 0, "skips": 0, "signals": 0, "candidates": 0,
             "fetched": 0, "pending": 0, "latency": []}
    if board is None:
        return stats

    # --- which (sector, side) pairs are open on this bar?
    sec_sides = {}
    for sec in board.sectors:
        sides = [s for s in ("BUY", "SELL") if board.entry_ok(s, sec)[0]]
        if sides:
            sec_sides[sec] = sides
    if not sec_sides:
        return stats
    cands = sorted({sym for sec in sec_sides for sym in SEC_STOCKS.get(sec, [])})
    stats["candidates"] = len(cands)

    # --- stage 2a: fetch only what stage 1 did not already pull
    missing = [s for s in cands if not (s in bars_map and bars_map[s] is not None)]
    if missing and fetch_missing:
        got = fetch_symbols(missing, now)
        stats["fetched"] = len(missing)
        for sym, b in got.items():
            if b is not None:
                bars_map[sym] = b

    target_bar = (pd.Timestamp(close_ts) - pd.Timedelta(minutes=CANDLE_MIN)).strftime("%H:%M")
    ready_lead, ready_lag, pending = [], [], []
    for sym in cands:
        b = bars_map.get(sym)
        if b is None:
            pending.append(sym)
            continue
        closed = _closed_bars(b, close_ts)
        if closed is None or closed.empty:
            pending.append(sym)
            continue
        new = _new_bars(st, sym, closed)
        if not new:
            continue
        latest = new[-1]
        if not _has_bar(closed, latest):
            continue
        lag = ((now_ist() if now is None else now) - _bar_close_ts(latest, today)).total_seconds()
        if lag > max_lag:
            _advance_cursor(st, sym, closed)
            _add_skip(st, {"symbol": sym, "side": None, "signal": "—", "time": latest,
                           "entry": None, "sector": SECTOR_OF.get(sym),
                           "why": f"stale bar {latest} ({lag:.0f}s > {max_lag}s) — not alerted"})
            stats["skips"] += 1
            continue
        if L.sym_has_open(st, sym):
            _advance_cursor(st, sym, closed)      # 1-open-trade rule; no engine work needed
            continue
        sides = sec_sides.get(SECTOR_OF.get(sym)) or []
        if len(sides) == 1:
            cache = sym_cache.get(sym) or {}
            pc = cache.get("prev_close")
            if pc:
                px = float(closed.loc[closed["t"] == latest, "close"].iloc[-1])
                ok, why = lead_ok(board, SECTOR_OF.get(sym), sides[0], (px / pc - 1) * 100.0)
                if not ok:
                    ready_lag.append((sym, latest, why))
                    continue
        ready_lead.append((sym, latest))

    def _process(work, allow_entries=True):
        """Run the engine on `work` and dispatch entries / skips."""
        jobs, metas = [], {}
        for sym, latest in work:
            closed = _closed_bars(bars_map[sym], close_ts)
            cache = sym_cache.get(sym) or symbol_history(sym, today)
            if not cache.get("ok"):
                _advance_cursor(st, sym, closed)
                continue
            metas[sym] = latest
            jobs.append((sym, engine_frame(cache, closed)))

        def handle(sym, res):
            latest = metas.get(sym)
            if latest is None:
                return
            closed = _closed_bars(bars_map[sym], close_ts)
            _advance_cursor(st, sym, closed)
            for idx, code, name in _signal_rows(res, today):
                etime = pd.Timestamp(idx).tz_convert(IST).strftime("%H:%M") \
                    if getattr(idx, "tzinfo", None) is not None else pd.Timestamp(idx).strftime("%H:%M")
                if etime < latest:
                    if etime >= L.CHART_MIN_TIME:  # bar discovered after its moment — never alert
                        _add_skip(st, {"symbol": sym, "side": "BUY" if code < 200 else "SELL",
                                       "signal": name, "time": etime,
                                       "entry": None, "sector": SECTOR_OF.get(sym),
                                       "why": f"discovered late ({etime} < {latest}) — not alerted"})
                    continue
                if etime > latest:
                    latest = etime                # engine saw a newer bar than we tracked
                    metas[sym] = etime
                stats["signals"] += 1
                side = "BUY" if code < 200 else "SELL"
                sec = SECTOR_OF.get(sym)
                why = None
                if code in (90, 290):
                    why = "scanner-table preview (90/290) — no chart label"
                elif etime < L.CHART_MIN_TIME:
                    why = f"before {L.CHART_MIN_TIME} chart window"
                elif etime >= trader.SQOFF:
                    why = f"at/after {trader.SQOFF} square-off — no entry"
                elif sec not in board.sectors:
                    why = "sector home missing from board (strict)"
                else:
                    ok, why2 = board.entry_ok(side, sec)
                    if ok:
                        cache = sym_cache.get(sym) or {}
                        pc = cache.get("prev_close")
                        if pc:
                            px = float(closed.loc[closed["t"] == etime, "close"].iloc[-1])
                            lok, ltxt = lead_ok(board, sec, side, (px / pc - 1) * 100.0)
                            if lok:
                                why2 = f"{why2} · {ltxt}"
                            else:
                                ok, why2 = False, ltxt
                    if not ok:
                        why = why2
                entry = float(closed.loc[closed["t"] == etime, "close"].iloc[-1])
                if why:
                    print(f"  M6 {sym} {side} {name} @ {etime} — SKIPPED: {why}")
                    if _add_skip(st, {"symbol": sym, "side": side, "signal": name, "time": etime,
                                      "entry": round(entry, 2), "sector": sec, "why": why}):
                        stats["skips"] += 1
                    continue
                if not allow_entries:
                    continue
                # freshness re-check immediately before the alert goes out
                sent_at = now_ist() if now is None else now
                bar_lag = (sent_at - _bar_close_ts(etime, today)).total_seconds()
                if bar_lag < 0 or bar_lag > max_lag:
                    _add_skip(st, {"symbol": sym, "side": side, "signal": name, "time": etime,
                                   "entry": round(entry, 2), "sector": sec,
                                   "why": f"stale at send ({bar_lag:.0f}s) — not alerted"})
                    stats["skips"] += 1
                    continue
                tr = trader.evaluate(sym, side, etime, entry, name, closed,
                                     warmup=(sym_cache.get(sym) or {}).get("warmup"))
                if "error" in tr:
                    print(f"  M6 {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                    continue
                tr["sector"] = sec
                st.setdefault("live_cycles", int(st.get("live_cycles") or 0))
                tkey, k = sym, 2
                while tkey in st["trades"]:
                    tkey = f"{sym}#{k}"
                    k += 1
                st["trades"][tkey] = tr
                st["alerts"].append(f"{tkey}:ENTRY")
                save_state(st, dry_run)           # persisted BEFORE the alert (no duplicates)
                suffix = f" · #{k - 1} on {sym}" if tkey != sym else ""
                send("🅼6 · " + trader.fmt_alert(tr, "ENTRY")
                     + f"\n🏭 sector <b>{sec}</b> — {why2}{suffix}"
                     + f"\n⏱ bar {etime} closed {sent_at.strftime('%H:%M:%S')} IST (+{bar_lag:.1f}s)",
                     dry_run=dry_run)
                stats["entries"] += 1
                stats["latency"].append(round(bar_lag, 2))
                print(f"  >>> M6 ENTRY {tkey} {side} @ {entry} [{sec}] (+{bar_lag:.1f}s) ({why2})")

        run_engine_jobs(jobs, on_result=handle)

    # entries first (latency critical), then the laggard audit, then late arrivals
    _process(ready_lead, allow_entries=True)
    if ready_lag:
        _process([(s, t) for s, t, _w in ready_lag], allow_entries=False)
    if pending:
        stats["pending"] = len(pending)
        _poll_pending(st, pending, bars_map, target_bar, close_ts, max_lag, stats, now)
        newly = []
        for sym in pending:
            b = bars_map.get(sym)
            if b is None:
                continue
            closed = _closed_bars(b, close_ts)
            new = _new_bars(st, sym, closed) if closed is not None and len(closed) else []
            if not new:
                continue
            latest = new[-1]
            lag = ((now_ist() if now is None else now) - _bar_close_ts(latest, today)).total_seconds()
            if lag < 0 or lag > max_lag:
                _advance_cursor(st, sym, closed)
                continue
            if L.sym_has_open(st, sym):
                _advance_cursor(st, sym, closed)
                continue
            newly.append((sym, latest))
        if newly:
            print(f"  late arrivals ({len(newly)}): {', '.join(s for s, _ in newly)}")
            _process(newly, allow_entries=True)
    return stats


def _poll_pending(st, pending, bars_map, target_bar, close_ts, max_lag, stats, now=None):
    """Delta-poll laggards: 0.75 s → 1.5 s → 3 s until the freshness deadline."""
    deadline = pd.Timestamp(close_ts).to_pydatetime() + dt.timedelta(seconds=max_lag)
    for delay in (0.75, 1.5, 3.0, 3.0):
        todo = [s for s in pending if not _has_bar(_closed_bars(bars_map.get(s), close_ts), target_bar)]
        if not todo or now_ist() >= deadline:
            return
        time.sleep(delay)
        got = fetch_symbols(todo, now)
        for sym, b in got.items():
            if b is not None:
                bars_map[sym] = b


# --------------------------------------------------------------- EOD
def run_eod(st, today, now, board=None, bars_map=None, dry_run=False, reason="eod"):
    """EOD report + Telegram summary. State is marked done BEFORE sending."""
    if st.get("eod_done"):
        return False
    bars_map = bars_map or {}
    try:
        done = [t for t in st.get("trades", {}).values() if isinstance(t, dict) and "symbol" in t]
        dlbl = now.strftime("%d-%b-%Y") + " (M6: sector-flow gate)"
        sk = {}
        for it in st.get("skipped", []):
            sk.setdefault(it["why"], []).append(
                [it["symbol"], it["side"], it["signal"] + f" [{it.get('sector') or '-'}]",
                 it["time"], it["entry"]])
        out = None
        try:
            out = report.build(done, dlbl, st.get("gate") or {"status": "?", "source": "?"},
                               str(ROOT / f"paper_test_M6_{today}.xlsx"), skipped=sk or None)
        except Exception as e:
            print(f"  M6 report build: {type(e).__name__}: {e}")
        try:
            learn_log.harvest("M6", today, st, None, bars_map, board=board)
        except Exception as e:
            print(f"  M6 learn harvest: {type(e).__name__}: {e}")
        msg = "🅼6 EOD · " + report.summary_text(done, dlbl, st.get("gate") or {})
        if board is not None:
            try:
                msg += "\n\n" + FM.board_text(board)
            except Exception:
                pass
        st["eod_done"] = True
        st["eod_reason"] = reason
        save_state(st, dry_run)              # done BEFORE send → report can never double-send
        send(msg, dry_run=dry_run)
        if out:
            if dry_run:
                print(f"[DRY] document {out}")
            else:
                tg.send_document(out, caption=f"🅼6 📄 M6 sector-flow paper report {today}")
        print(f"M6 EOD ({reason}): {len(done)} trades · {len(st.get('skipped', []))} skipped")
        return True
    except Exception as e:
        print(f"  M6 EOD: {type(e).__name__}: {e}")
        return False


# --------------------------------------------------------------- one cycle
def run_cycle(now=None, target_close=None, dry_run=False, st=None, today=None,
              git_sync_flag=False):
    """One next-second cycle for the candle that closed at `target_close`.

    Returns a summary dict; `eod` is True once the day is finished.
    """
    now = now or now_ist()
    today = today or now.strftime("%Y-%m-%d")
    if target_close is None:
        closes = [c for c in session_closes(now.date()) if c <= now]
        target_close = closes[-1] if closes else None
    if target_close is None:
        return {"status": "pre-market", "entries": 0}
    if isinstance(target_close, dt.datetime):
        close_ts = target_close
    else:
        close_ts = pd.Timestamp(target_close).to_pydatetime()
    target_bar = (pd.Timestamp(close_ts) - pd.Timedelta(minutes=CANDLE_MIN)).strftime("%H:%M")
    t0 = time.monotonic()
    lag_now = (now - close_ts).total_seconds()
    if st is None:
        st = load_state(today)
    if st.get("eod_done"):
        return {"status": "eod-done", "entries": 0}
    if now.strftime("%Y-%m-%d") != today:
        return {"status": "date-changed", "entries": 0}
    # Hard post-close gate: a cycle started after the session (the 18:00–18:24
    # production replay) must never evaluate a single bar, let alone alert.
    if now.time() > dt.time(15, 45) or (now.time() > dt.time(15, 35)
                                        and int(st.get("live_cycles") or 0) == 0):
        _mark_eod_silent(st, dry_run, f"post-close start at {now.strftime('%H:%M')} — refused")
        return {"status": "refused", "entries": 0, "bar": target_bar}
    # A candle older than the exit budget (missed trigger, resumed laptop, late
    # cron) is stale work: never evaluate it, never alert from it.
    if lag_now > max(MAX_LAG, EXIT_LAG):
        print(f"M6 {target_bar}: cycle refused ({lag_now:.0f}s after the close, "
              f"budget {max(MAX_LAG, EXIT_LAG)}s) — no stale evaluation")
        return {"status": "stale", "entries": 0, "bar": target_bar}

    feeds.reset_feed_stats()
    sym_cache = {sym: symbol_history(sym, today) for sym in L.SYMS}

    # --- 1. readiness: never scan before the gate index has the closed bar
    nif_bars, waited = wait_target_ready(target_bar, close_ts, MAX_LAG)
    t_ready = time.monotonic()
    if nif_bars is None or not _has_bar(_today_only(nif_bars, today), target_bar):
        print(f"M6 {target_bar}: ^NSEI bar not published after {waited:.1f}s — running with "
              f"whatever is closed (freshness guards still apply)")

    # --- 2. one parallel fetch for the whole board + open trades
    open_syms = [k.split("#")[0] for k in st.get("trades", {})]
    synth_syms = sorted({s for m in MEMBERS.values() for s in m})
    idx_tickers = list(FM.OFFICIAL_TICKERS.values()) + [FM.VIX_TICKER]

    # one parallel batch: 18 index slices + the 64 basket members (+ open trades)
    idx_live = fetch_indices(idx_tickers)
    bars_map = {s: b for s, b in fetch_symbols(sorted(set(synth_syms) | set(open_syms)), now).items()
                if b is not None}
    # keep the freshly polled ^NSEI (no second request)
    if nif_bars is not None and len(nif_bars):
        idx_live[FM.NIFTY_TICKER] = nif_bars
    # Yahoo drops the 15:20 bar on ~98% of NSE symbols — rebuild it from the
    # 15:25 open so the 15:25 square-off candle always exists for the trader.
    bars_map = {s: _ensure_sqoff_bar(b, today) for s, b in bars_map.items()}

    # --- 3. delta retries for indices/basket members that lagged the close
    idx_tickers_all = idx_tickers + [FM.NIFTY_TICKER]
    for attempt in range(3):
        lag_idx = [t for t in idx_tickers_all if not _has_bar(_closed_bars(_today_only(_prep(idx_live.get(t)), today), close_ts), target_bar)]
        lag_sym = [s for s in synth_syms if not _has_bar(_closed_bars(bars_map.get(s), close_ts), target_bar)]
        if not lag_idx and not lag_sym:
            break
        if now_ist() >= close_ts + dt.timedelta(seconds=MAX_LAG):
            break
        time.sleep(0.75)
        if lag_idx:
            idx_live.update(fetch_indices(lag_idx))
        if lag_sym:
            for sym, b in fetch_symbols(lag_sym).items():
                if b is not None:
                    bars_map[sym] = b
        if attempt:
            print(f"  delta retry {attempt + 1}: {len(lag_idx)} indices · {len(lag_sym)} stocks still lagging")
    t_fetch = time.monotonic()

    # --- 4. exact 30-sector board as of the candle close
    board, meta = build_board(close_ts, today, bars_map, idx_live, sym_cache)
    gate_ok = board is not None
    st["gate"] = {"status": "OK" if gate_ok else "OFFLINE", "source": meta}
    if gate_ok:
        st["sector_board"] = {"time": target_bar, "net_tilt": board.net_tilt,
                              "tilt": board.tilt_label,
                              "bull_q": board.bull_q[:4], "bear_q": board.bear_q[:4],
                              "persist_bull": board.persist_bull,
                              "persist_bear": board.persist_bear,
                              "hold": board.hold_mode, "held_bull": board.held_bull,
                              "held_bear": board.held_bear}
    print(f"M6 {target_bar} board: {meta} ({'OK' if gate_ok else 'OFFLINE'})")

    # --- 5. open trades FIRST (fresh exits are as urgent as entries)
    exits = manage_trades(st, today, close_ts, now, bars_map, sym_cache, dry_run=dry_run)

    # --- 6. entries
    stats = scan_entries(st, today, close_ts, now, board, bars_map, sym_cache,
                         dry_run=dry_run) if gate_ok else {"entries": 0, "skips": 0,
                                                           "signals": 0, "latency": []}
    t_scan = time.monotonic()

    # --- 7. heartbeat / diagnostics
    latency = (now - close_ts).total_seconds()
    st.setdefault("latency", []).append(round(latency, 2))
    st["latency"] = st["latency"][-120:]
    st["cycles"] = int(st.get("cycles") or 0) + 1
    st["live_cycles"] = int(st.get("live_cycles") or 0) + 1
    st["last_cycle"] = {"bar": target_bar, "at": now.isoformat(),
                        "latency": round(latency, 2),
                        "entries": stats.get("entries", 0),
                        "feed": feeds.feed_stats()}
    if target_bar.endswith(":15") and target_bar >= "10:15":
        best = min(stats.get("latency") or [latency]) if stats.get("latency") else latency
        send(f"💓 🅼6 {target_bar} IST · {len(st['trades'])} trades · "
             f"board {'OK' if gate_ok else 'OFFLINE'} · cycle +{latency:.1f}s · "
             f"alert +{best:.1f}s", silent=True, dry_run=dry_run)
    save_state(st, dry_run)

    # --- 8. EOD after the last candle of the session
    eod = False
    if now.time() >= LAST_CLOSE:
        eod = run_eod(st, today, now, board=board, bars_map=bars_map, dry_run=dry_run,
                      reason="session-close")

    print(f"M6 cycle {target_bar}: ready {t_ready - t0:.2f}s · fetch {t_fetch - t_ready:.2f}s · "
          f"scan {t_scan - t_fetch:.2f}s · total {time.monotonic() - t0:.2f}s · "
          f"cycle +{latency:.1f}s · entries +{stats.get('entries', 0)} · "
          f"signals {stats.get('signals', 0)} · skips {stats.get('skips', 0)} · exits {exits}")

    if git_sync_flag or st.get("git_sync"):
        nxt, _ = next_wake_time(now_ist(), WAKE_OFFSET)
        git_sync(f"{target_bar}", dry_run=dry_run, next_close=nxt)

    return {"status": "ok", "bar": target_bar, "entries": stats.get("entries", 0),
            "skips": stats.get("skips", 0), "signals": stats.get("signals", 0),
            "exits": exits, "latency": round(latency, 2), "eod": eod,
            "board": board, "bars": bars_map}


# --------------------------------------------------------------- session loop
def _weekday_ok(day, override_env="M6_ALLOW_WEEKEND"):
    return day.weekday() < 5 or os.environ.get(override_env) == "1"


def _mark_eod_silent(st, dry_run, reason):
    st["eod_done"] = True
    st["eod_reason"] = reason
    save_state(st, dry_run)
    print(f"M6: {reason} — EOD marked done silently (zero Telegram sends)")


def cmd_session(dry_run=False, offset=None, max_runtime_min=324, loop=None,
                forever=False, wait_for_bar=False, git_sync_flag=False,
                preload_only=False, once=False):
    offset = WAKE_OFFSET if offset is None else offset
    start = now_ist()
    today = start.strftime("%Y-%m-%d")
    if not _weekday_ok(start.date()):
        print(f"M6: {today} is a weekend — idle (set M6_ALLOW_WEEKEND=1 to override).")
        return 0
    preload(today)
    if preload_only:
        return 0
    try:
        st = load_state(today)
    except StateCorrupt as e:
        print(f"M6 FAIL-CLOSED: {e} — refusing to trade or alert on unknown state.")
        return 1
    if once:
        out = run_cycle(now=now_ist(), dry_run=dry_run, st=st, today=today,
                        git_sync_flag=git_sync_flag)
        print(f"M6 once: {out.get('status')} entries={out.get('entries')} bar={out.get('bar')}")
        return 0

    deadline = start + dt.timedelta(minutes=max(1.0, float(max_runtime_min)))
    cycles = 0
    while True:
        now = now_ist()
        if st.get("eod_done"):
            print("M6: EOD done — exiting session.")
            break
        # ---- post-close / late-start guards
        if now.time() >= LAST_CLOSE:
            if now.time() <= dt.time(15, 35):
                out = run_cycle(now=now_ist(), target_close=session_closes(now.date())[-1],
                                dry_run=dry_run, st=st, today=today, git_sync_flag=git_sync_flag)
                cycles += 1
                if out.get("eod"):
                    break
                continue
            if now.time() <= dt.time(15, 45):
                has_trades = any(isinstance(t, dict) and "symbol" in t
                                 for t in st.get("trades", {}).values())
                if int(st.get("live_cycles") or 0) > 0 and has_trades:
                    run_eod(st, today, now, dry_run=dry_run, reason="recovery")
                else:
                    _mark_eod_silent(st, dry_run, "post-close start without live cycles")
                break
            _mark_eod_silent(st, dry_run, "after 15:45 with no session")
            break
        if now > deadline:
            print(f"M6: max runtime reached at {now.strftime('%H:%M:%S')} IST — handing over.")
            break
        wake, close = next_wake_time(now, offset, allow_rollover=forever)
        if wake is None:
            print("M6: session over — no further candle today.")
            break
        if wake.date() != now.date():
            # --forever: prepare the next session off the clock (never at 09:20:01)
            prep = dt.datetime.combine(wake.date(), dt.time(9, 10), tzinfo=IST)
            if not _weekday_ok(wake.date()):
                _sleep_until(wake + dt.timedelta(hours=24), deadline=None)
                continue
            _sleep_until(prep)
            today = wake.date().strftime("%Y-%m-%d")
            preload(today)
            st = load_state(today)
            continue
        if loop is not None and cycles >= int(loop):
            break
        if wake > deadline:
            print(f"M6: next candle {wake.strftime('%H:%M:%S')} after max runtime — stopping.")
            break
        if not _sleep_until(wake, deadline=deadline):
            print("M6: max runtime reached while waiting — stopping.")
            break
        if loop is not None and cycles >= int(loop):
            break
        out = run_cycle(now=now_ist(), target_close=close, dry_run=dry_run, st=st,
                        today=today, git_sync_flag=git_sync_flag)
        cycles += 1
        if out.get("eod"):
            break
        if wait_for_bar:
            break
    print(f"M6 session finished: {cycles} cycle(s) · {len(st.get('trades', {}))} trades · "
          f"eod={st.get('eod_done')}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="M6 sector-flow paper trader (next-second alerts)")
    ap.add_argument("--session", action="store_true", help="aligned session loop (default)")
    ap.add_argument("--forever", action="store_true", help="run every session, day after day")
    ap.add_argument("--loop", type=int, default=None, help="run N aligned candle cycles")
    ap.add_argument("--once", action="store_true", help="run one cycle immediately")
    ap.add_argument("--wait-for-bar", action="store_true", help="wait for the next candle, run once")
    ap.add_argument("--wake-offset-sec", type=float, default=None,
                    help=f"seconds after candle close to wake (default {WAKE_OFFSET:.1f})")
    ap.add_argument("--max-runtime-min", type=float, default=324.0)
    ap.add_argument("--git-sync", action="store_true", help="push M6 state/report after alerts")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, never send or write state")
    ap.add_argument("--test-alert", action="store_true", help="send a Telegram self-test and exit")
    ap.add_argument("--preload-only", action="store_true", help="warm caches and exit")
    a = ap.parse_args(argv)

    if a.test_alert:
        return test_alert(dry_run=a.dry_run)
    if a.git_sync:
        os.environ["M6_GIT_SYNC"] = "1"
    if a.once:
        return cmd_session(dry_run=a.dry_run, offset=a.wake_offset_sec, once=True,
                           git_sync_flag=a.git_sync)
    return cmd_session(dry_run=a.dry_run, offset=a.wake_offset_sec,
                       max_runtime_min=a.max_runtime_min,
                       loop=a.loop if a.loop is not None else (1 if a.wait_for_bar else None),
                       forever=a.forever, wait_for_bar=a.wait_for_bar,
                       git_sync_flag=a.git_sync, preload_only=a.preload_only)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StateCorrupt as exc:
        print(f"M6 FAIL-CLOSED: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("M6 interrupted")
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
