"""Previous-session close/pivot seeding shared by M12 / M13 / M14.

Why this exists
---------------
The EOD seed inside each runner reads the *intraday* bars_map of the final
15:25-15:35 IST cycle. When Yahoo (the fallback feed) lags more than ~15
minutes, fewer than the required 180 of 210 symbols have a >=15:20 bar at that
moment, the seed refuses to write, and the next session starts STALE ->
strict no-entry. That is exactly what silenced M12/M13 on 2026-08-28/31.

This module rebuilds the previous-close cache from the *daily* 5-minute
history (`fetch_bars_yahoo(sym, "5d")`), which is complete and final shortly
after the session close. It is used two ways:

1. `top_up()` / `daily_prev_context()` from each runner's EOD seed — fills the
   symbols the intraday map missed (quality floor: the day's final bar must be
   >= 15:20, i.e. an official close, never a 15:10/15:15 mark).
2. CLI `python -u seed_prev_context.py [--models m12,m13,m14] [--date D]` —
   the post-close reseed run by the live workflows at 16:10 IST, and usable
   manually any time a cache goes missing or stale.

Cache contract (all three files under data/):
  {"date", "close": {sym: px}, "pivot": {sym: px}, "count",
   "last_bar_min", "total_fed", "generated_utc", "status": "OK", "source"}

A partial (< --min-count symbols) result is NEVER written over an existing
cache: tomorrow fails closed instead of trading on a poisoned baseline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

import pandas as pd

import feeds
import live_runner as L

ROOT = L.ROOT
IST = dt.timezone(dt.timedelta(hours=5, minutes=30), name="IST")

CACHE_FILES = {
    "m12": ROOT / "data" / "m12_prev_close.json",
    "m13": ROOT / "data" / "m13_prev_context.json",
    "m14": ROOT / "data" / "m14_prev_context.json",
}

MIN_COUNT_DEFAULT = 180  # of ~210 F&O symbols — matches every runner's guard


def now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def expected_previous_weekday(td: dt.date) -> dt.date:
    d = td - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def parse_day(s) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        return None


def latest_closed_session(now: dt.datetime | None = None,
                          close_hhmm: str = "15:45") -> dt.date:
    """Most recent NSE session that has fully closed by `now` (IST)."""
    now = now or now_ist()
    today = now.date()
    hhmm = now.strftime("%H:%M")
    return today if hhmm >= close_hhmm else expected_previous_weekday(today)


def session_ohlc(df: pd.DataFrame, day) -> dict | None:
    """Daily OHLC of `day` extracted from a 5-minute bar frame.

    Accepts tz-aware or naive `dt` columns (history CSVs carry +05:30 strings).
    """
    day = parse_day(day)
    if day is None or df is None or df.empty:
        return None
    d = df.copy()
    try:
        d["dt"] = pd.to_datetime(d["dt"], utc=True).dt.tz_convert("Asia/Kolkata")
    except (TypeError, ValueError):
        return None
    d["ymd"] = d["dt"].dt.strftime("%Y-%m-%d")
    q = d[d["ymd"] == day.isoformat()].sort_values("dt")
    if q.empty:
        return None
    close = float(q["close"].iloc[-1])
    if not (close > 0):
        return None
    high = float(q["high"].max())
    low = float(q["low"].min())
    return {
        "close": close,
        "high": high,
        "low": low,
        "pivot": (high + low + close) / 3.0,
        "bar_min": str(q["dt"].dt.strftime("%H:%M").iloc[-1]),
    }


def daily_prev_context(symbols: Iterable[str], day,
                       fetch: Callable[[str, str], pd.DataFrame | None] | None = None,
                       min_bar: str | None = "15:20",
                       sleep_s: float = 0.12,
                       deadline_s: float = 420.0) -> dict:
    """Official close/pivot maps for `day` pulled from the daily 5m history.

    `min_bar="15:20"` keeps the quality floor used by the runners' EOD seeds:
    a session whose final bar is a 15:10/15:15 mark is not an official close.
    Pass `min_bar=None` to accept any final bar (already-finalised daily data).
    """
    fetch = fetch or feeds.fetch_bars_yahoo
    deadline = time.monotonic() + max(0.0, deadline_s)
    closes: dict[str, float] = {}
    pivots: dict[str, float] = {}
    bar_mins: list[str] = []
    tried = 0
    for sym in symbols:
        if time.monotonic() > deadline:
            break
        tried += 1
        try:
            df = fetch(sym, "5d")
        except Exception as exc:
            print(f"  seed {sym}: {type(exc).__name__}: {exc}")
            df = None
        if df is None or df.empty:
            continue
        try:
            ohlc = session_ohlc(df, day)
        except Exception as exc:
            print(f"  seed {sym}: bad frame ({type(exc).__name__}: {exc})")
            continue
        if ohlc is None:
            continue
        if min_bar and ohlc["bar_min"] < min_bar:
            continue
        closes[sym] = ohlc["close"]
        pivots[sym] = ohlc["pivot"]
        bar_mins.append(ohlc["bar_min"])
        if sleep_s:
            time.sleep(sleep_s)
    return {
        "date": str(parse_day(day) or day),
        "close": closes,
        "pivot": pivots,
        "count": len(closes),
        "tried": tried,
        "last_bar_min": min(bar_mins) if bar_mins else None,
    }


def top_up(vals: dict[str, float], pivots: dict[str, float], today,
           all_symbols: Iterable[str], **kw) -> tuple[dict[str, float], dict[str, float], int]:
    """Fill the symbols missing from an intraday EOD seed (runners' hot path).

    Returns (closes, pivots, added) with the daily-fetched symbols merged in.
    Set env SEED_PREV_DAILY_TOPUP=0 to disable the network top-up (tests,
    incident response): the call then becomes a no-op.
    """
    if os.environ.get("SEED_PREV_DAILY_TOPUP", "1") == "0":
        return vals, pivots, 0
    missing = [s for s in all_symbols if s not in vals]
    if not missing:
        return vals, pivots, 0
    ctx = daily_prev_context(missing, today, **kw)
    got_close: Mapping[str, float] = ctx.get("close") or {}
    got_pivot: Mapping[str, float] = ctx.get("pivot") or {}
    vals.update(got_close)
    pivots.update(got_pivot)
    return vals, pivots, len(got_close)


def write_cache(model: str, day, closes: Mapping[str, float],
                pivots: Mapping[str, float], last_bar_min,
                total_fed: int, source: str,
                min_count: int = MIN_COUNT_DEFAULT) -> dict:
    """Write data/<model> cache. Partial data is never persisted."""
    path = CACHE_FILES[model]
    meta = {
        "date": str(day),
        "count": len(closes),
        "last_bar_min": last_bar_min,
        "total_fed": total_fed,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
    }
    if len(closes) < min_count:
        meta["status"] = "INSUFFICIENT"
        meta["policy"] = "cache not written; existing cache left untouched"
        print(f"seed_prev_context[{model}]: {len(closes)}/{min_count} symbols — not written")
        return meta
    meta.update(close=dict(closes), pivot=dict(pivots), status="OK")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=1))
    print(f"seed_prev_context[{model}]: wrote {len(closes)} closes ({day}) -> {path.name}")
    return meta


def seed_models(models: Iterable[str], day, min_count: int = MIN_COUNT_DEFAULT,
                min_bar: str | None = "15:20", deadline_s: float = 420.0,
                symbols: Iterable[str] | None = None, fetch=None) -> dict:
    """Rebuild every requested model's prev-close cache for `day`."""
    symbols = list(symbols if symbols is not None else L.SYMS)
    out: dict[str, dict] = {}
    for model in models:
        ctx = daily_prev_context(symbols, day, fetch=fetch, min_bar=min_bar,
                                 deadline_s=deadline_s)
        out[model] = write_cache(model, ctx["date"], ctx["close"], ctx["pivot"],
                                 ctx["last_bar_min"], ctx["tried"],
                                 source="daily history seed", min_count=min_count)
        out[model]["last_bar_min_observed"] = ctx["last_bar_min"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="m12,m13,m14",
                    help="comma-separated subset of m12,m13,m14 (default: all)")
    ap.add_argument("--date", default=None,
                    help="session whose closes to record, YYYY-MM-DD "
                         "(default: latest fully-closed IST session)")
    ap.add_argument("--min-count", type=int, default=MIN_COUNT_DEFAULT)
    ap.add_argument("--min-bar", default="15:20",
                    help="quality floor for the day's final bar (default 15:20); "
                         "'none' accepts any final bar")
    ap.add_argument("--deadline-seconds", type=float, default=420.0)
    args = ap.parse_args()

    models = [m.strip().lower() for m in args.models.split(",") if m.strip()]
    bad = [m for m in models if m not in CACHE_FILES]
    if bad:
        ap.error(f"unknown model(s): {','.join(bad)} (choose from m12,m13,m14)")

    day = parse_day(args.date) or latest_closed_session()
    min_bar = None if args.min_bar.lower() in ("none", "any") else args.min_bar
    print(f"seed_prev_context: models={models} day={day} min_count={args.min_count} "
          f"min_bar={min_bar or 'any'}")
    summary = seed_models(models, day, min_count=args.min_count, min_bar=min_bar,
                          deadline_s=args.deadline_seconds)
    print(json.dumps({m: {k: v for k, v in s.items() if k != "close" and k != "pivot"}
                      for m, s in summary.items()}, indent=1))
    if any(s.get("status") != "OK" for s in summary.values()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
