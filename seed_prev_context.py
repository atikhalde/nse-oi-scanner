"""Previous-session close/pivot seeding shared by M12 / M13 / M14.

Why this exists
---------------
The EOD seed inside each runner reads the *intraday* bars_map of the final
15:25-15:35 IST cycle. When Yahoo (the fallback feed) lags more than ~15
minutes, fewer than the required 180 of 210 symbols have a >=15:20 bar at that
moment, the seed refuses to write, and the next session starts STALE ->
strict no-entry. That is exactly what silenced M12/M13 on 2026-08-28/31.

2026-08-31 root cause addendum — the "3/210" was NOT a Yahoo outage
-------------------------------------------------------------------
Finalised Yahoo 5-minute data for NSE simply *ends* at 15:10-15:15 for ~99%
of symbols (measured on 2026-08-28: 73 files end 15:10, 134 end 15:15, only 3
end 15:25). The old >=15:20 "official close" floor therefore rejected
everything except those ~3 symbols on EVERY path (EOD seed, daily top-up,
self-heal, post-close reseed), starving M12/M13/M14 of a baseline while M11 —
which reads the previous day straight from local history with no bar-time
floor — kept trading. The floor is now `CLOSE_BAR_FLOOR = "15:05"`: a last bar
at/after 15:05 of a closed session is the official close (it matches what the
runners' history-bootstrap fallback and M11 have always used), while a
truncated mid-afternoon snapshot (e.g. a 14:30 last bar from a lagged fetch)
is still rejected.

This module now prefers the LOCAL daily history (`data/history/*.csv`,
refreshed every morning at 08:45 IST by the `2. Bootstrap` workflow) over live
Yahoo fetches — `use_local=True` — so rebuilding a baseline costs zero network
calls and cannot be rate limited. It is used three ways:

1. `top_up()` / `daily_prev_context()` from each runner's EOD seed — fills the
   symbols the intraday map missed from the daily 5-minute history.
2. `self_heal()` from each runner's first cycle — rebuilds a missing/stale
   cache for the previous session from local history first, network second.
3. CLI `python -u seed_prev_context.py [--models m12,m13,m14] [--date D]` —
   the post-close reseed run by the live workflows at 16:10 IST (cron
   `40 10 * * 1-5`), and usable manually any time a cache goes missing.

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

# A session's last bar at/after this time counts as the official close.
# Yahoo's NSE 5-minute data ends at 15:10/15:15 for ~99% of symbols (there is
# no 15:20/15:25 bucket after the 15:15 candle for most names), so the old
# 15:20 floor rejected nearly every symbol — the "3/210 cache" outage.
CLOSE_BAR_FLOOR = "15:05"


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


def local_prev_context(symbols: Iterable[str], day,
                       min_bar: str | None = CLOSE_BAR_FLOOR,
                       hist_dir=None) -> dict:
    """Official close/pivot maps for `day` read from the LOCAL daily history.

    `data/history/*.csv` is refreshed every morning (08:45 IST) by the
    `2. Bootstrap` workflow, so for any *past* session this needs zero network
    calls and is immune to Yahoo rate limiting — the exact failure that
    starved every network-only seeding path on 2026-08-31.

    Same return contract as `daily_prev_context`. Symbols without a history
    file or without the requested session are simply absent from `close`.
    """
    hist = Path(hist_dir) if hist_dir else L.HIST
    closes: dict[str, float] = {}
    pivots: dict[str, float] = {}
    bar_mins: list[str] = []
    tried = 0
    for sym in symbols:
        fp = hist / f"{sym}.csv"
        if not fp.exists():
            continue
        tried += 1
        try:
            df = pd.read_csv(fp, usecols=["dt", "high", "low", "close"])
            ohlc = session_ohlc(df, day)
        except Exception:
            continue
        if ohlc is None:
            continue
        if min_bar and ohlc["bar_min"] < min_bar:
            continue
        closes[sym] = ohlc["close"]
        pivots[sym] = ohlc["pivot"]
        bar_mins.append(ohlc["bar_min"])
    return {
        "date": str(parse_day(day) or day),
        "close": closes,
        "pivot": pivots,
        "count": len(closes),
        "tried": tried,
        "last_bar_min": min(bar_mins) if bar_mins else None,
    }


def daily_prev_context(symbols: Iterable[str], day,
                       fetch: Callable[[str, str], pd.DataFrame | None] | None = None,
                       min_bar: str | None = CLOSE_BAR_FLOOR,
                       sleep_s: float = 0.12,
                       deadline_s: float = 420.0,
                       rng: str = "5d",
                       retries: int = 2,
                       use_local: bool = False) -> dict:
    """Official close/pivot maps for `day` pulled from the daily 5m history.

    `min_bar` defaults to `CLOSE_BAR_FLOOR` ("15:05"): Yahoo's final NSE bar
    is a 15:10/15:15 mark for ~99% of symbols, which the old 15:20 floor
    rejected (the "3/210" outage). A truncated mid-afternoon snapshot (e.g. a
    14:30 last bar) is still rejected. Pass `min_bar=None` to accept any final
    bar (already-finalised daily data).

    `use_local=True` serves every symbol found in `data/history/*.csv` from
    disk (see `local_prev_context`) and only fetches the remainder from the
    network — with a fresh morning bootstrap that is zero Yahoo calls.

    `rng` is the Yahoo range passed to the fetcher. "5d" is enough for a
    consecutive trading day, but a Monday or a post-holiday session needs a
    wider window to still contain the previous session — the self-heal path
    uses "1mo" for exactly that reason.

    `retries` re-attempts a failed symbol with a short backoff. Yahoo rate
    limits (429) under load, and a single transient miss used to drop the
    symbol from the seed permanently; with Dhan's token dead every symbol
    depends on Yahoo, so one 429 storm emptied the whole cache.
    """
    symbols = list(symbols)
    closes: dict[str, float] = {}
    pivots: dict[str, float] = {}
    bar_mins: list[str] = []
    tried = 0

    if use_local and symbols:
        local = local_prev_context(symbols, day, min_bar=min_bar)
        closes.update(local["close"])
        pivots.update(local["pivot"])
        if local["last_bar_min"]:
            bar_mins.append(local["last_bar_min"])
        symbols = [s for s in symbols if s not in closes]
        tried += local["tried"]
        if not symbols:
            return {"date": str(parse_day(day) or day), "close": closes,
                    "pivot": pivots, "count": len(closes), "tried": tried,
                    "last_bar_min": min(bar_mins) if bar_mins else None,
                    "local_count": len(closes), "network_count": 0}

    fetch = fetch or feeds.fetch_bars_yahoo
    deadline = time.monotonic() + max(0.0, deadline_s)
    for sym in symbols:
        if time.monotonic() > deadline:
            print(f"  seed deadline hit after {tried} symbols "
                  f"({len(closes)} closes) — raise --deadline-seconds if this persists")
            break
        tried += 1
        df = None
        for attempt in range(max(1, retries)):
            if attempt and time.monotonic() > deadline:
                break
            try:
                df = fetch(sym, rng)
            except Exception as exc:
                print(f"  seed {sym}: {type(exc).__name__}: {exc}")
                df = None
            if df is not None and not df.empty:
                break
            if attempt + 1 < max(1, retries):
                time.sleep(0.4 * (attempt + 1))   # backoff before retrying
        if df is None or df.empty:
            print(f"  seed {sym}: no data after {max(1, retries)} attempt(s)")
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
                min_bar: str | None = CLOSE_BAR_FLOOR, deadline_s: float = 420.0,
                symbols: Iterable[str] | None = None, fetch=None,
                rng: str = "5d", use_local: bool = False) -> dict:
    """Rebuild every requested model's prev-close cache for `day`.

    One network pass is shared by all `models` — seeding m12/m13/m14 together
    costs 210 fetches, not 630, which matters when Yahoo is rate limiting.
    With `use_local=True` symbols found in `data/history/*.csv` cost no fetch
    at all; only the remainder goes to the network. `day` is always a fully
    closed session here (never the live one), so local history is authoritative
    whenever the morning bootstrap has landed.
    """
    symbols = list(symbols if symbols is not None else L.SYMS)
    out: dict[str, dict] = {}
    ctx = daily_prev_context(symbols, day, fetch=fetch, min_bar=min_bar,
                             deadline_s=deadline_s, rng=rng, use_local=use_local)
    local_n = ctx.get("local_count", 0) or 0
    source = ("local history seed" if local_n == len(symbols) and ctx["count"] == len(symbols)
              else f"local+daily history seed (local:{local_n})")
    for model in models:
        out[model] = write_cache(model, ctx["date"], ctx["close"], ctx["pivot"],
                                 ctx["last_bar_min"], ctx["tried"],
                                 source=source, min_count=min_count)
        out[model]["last_bar_min_observed"] = ctx["last_bar_min"]
        out[model]["date"] = ctx["date"]
    return out


def self_heal(model: str, today, deadline_s: float = 600.0,
              min_count: int = MIN_COUNT_DEFAULT,
              min_bar: str | None = CLOSE_BAR_FLOOR, fetch=None,
              use_local: bool = False) -> tuple[bool, dict]:
    """Rebuild `model`'s cache for the previous session from finalised daily data.

    This is the recovery path that was missing for M12/M13: their EOD seed only
    ran inside the 15:25 cycle, the in-runner self-heal window (15:36-16:30)
    was unreachable because their cron schedule stops at 15:35, and unlike M14
    they had no 16:10 post-close reseed job. One bad cache therefore silenced
    them permanently until someone rebuilt it by hand. All three now also get
    a 16:10 IST reseed cron in their live workflows.

    Safe to call every cycle — the caller gates it — because:
      * it targets the *previous* session, whose bars are final, so it cannot
        seed a live session with a partial day;
      * with `use_local=True` (what the runners pass) it reads the local
        `data/history` first, so a fresh bootstrap makes it a zero-network,
        rate-limit-proof operation;
      * `write_cache` refuses anything under `min_count`, so a bad run leaves
        the existing cache untouched instead of poisoning it;
      * the network remainder uses range="1mo" so a Monday / post-holiday
        session still finds the previous trading day inside the window.

    Returns (ok, meta). `ok` is True only when a >=min_count cache was written.
    """
    day = expected_previous_weekday(parse_day(today) or now_ist().date())
    print(f"seed_prev_context[{model}]: self-heal for {day} "
          f"(deadline {deadline_s:.0f}s, use_local={use_local})")
    try:
        summary = seed_models([model], day, min_count=min_count, min_bar=min_bar,
                              deadline_s=deadline_s, rng="1mo", fetch=fetch,
                              use_local=use_local)
    except Exception as exc:
        print(f"seed_prev_context[{model}]: self-heal failed "
              f"({type(exc).__name__}: {exc})")
        return False, {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}
    meta = summary.get(model) or {}
    return meta.get("status") == "OK", meta


def should_self_heal(st: dict, today: str, hhmm: str) -> bool:
    """Rate-limit guard: at most one self-heal attempt per model per hour.

    Without this a 5-minute ticker would fire 210 Yahoo fetches twelve times an
    hour, which is what gets the whole feed rate limited in the first place.
    Callers store the returned marker in their state dict.
    """
    marker = f"{today}:{str(hhmm)[:2]}"
    return st.get("prev_repair_marker") != marker


def mark_self_heal(st: dict, today: str, hhmm: str) -> None:
    st["prev_repair_marker"] = f"{today}:{str(hhmm)[:2]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--models", default="m12,m13,m14",
                    help="comma-separated subset of m12,m13,m14 (default: all)")
    ap.add_argument("--date", default=None,
                    help="session whose closes to record, YYYY-MM-DD "
                         "(default: latest fully-closed IST session)")
    ap.add_argument("--min-count", type=int, default=MIN_COUNT_DEFAULT)
    ap.add_argument("--min-bar", default=CLOSE_BAR_FLOOR,
                    help=f"quality floor for the day's final bar (default {CLOSE_BAR_FLOOR}); "
                         "'none' accepts any final bar")
    ap.add_argument("--deadline-seconds", type=float, default=420.0)
    ap.add_argument("--range", default="5d", dest="rng",
                    help="Yahoo range for the daily fetch (default 5d). Use 1mo "
                         "after a weekend/holiday so the window still contains "
                         "the previous session.")
    ap.add_argument("--no-local", action="store_true",
                    help="skip data/history and fetch every symbol from the network")
    args = ap.parse_args()

    models = [m.strip().lower() for m in args.models.split(",") if m.strip()]
    bad = [m for m in models if m not in CACHE_FILES]
    if bad:
        ap.error(f"unknown model(s): {','.join(bad)} (choose from m12,m13,m14)")

    day = parse_day(args.date) or latest_closed_session()
    min_bar = None if args.min_bar.lower() in ("none", "any") else args.min_bar
    print(f"seed_prev_context: models={models} day={day} min_count={args.min_count} "
          f"min_bar={min_bar or 'any'} local={'off' if args.no_local else 'first'}")
    summary = seed_models(models, day, min_count=args.min_count, min_bar=min_bar,
                          deadline_s=args.deadline_seconds, rng=args.rng,
                          use_local=not args.no_local)
    print(json.dumps({m: {k: v for k, v in s.items() if k != "close" and k != "pivot"}
                      for m, s in summary.items()}, indent=1))
    missing = [m for m in models if not summary.get(m)]
    if missing:
        print(f"seed_prev_context: no result for {','.join(missing)}")
        return 2
    # A model that could not reach min_count is the documented fail-closed
    # policy (existing cache left untouched, next session runs the history
    # bootstrap). It is a warning, not a workflow failure — exit 2 here used
    # to red-X the whole M14 post-close reseed run.
    insufficient = [m for m in models if summary[m].get("status") != "OK"]
    if insufficient:
        print(f"seed_prev_context: WARNING cache not written for "
              f"{','.join(insufficient)} (below min_count; existing cache "
              f"left untouched — fails closed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
