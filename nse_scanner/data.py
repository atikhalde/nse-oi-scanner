"""Bars for M15 — sourced from the shared nse-oi-scanner feed ladder.

M1–M6 (live_runner / m2 / m5 / m6 / fast_feed) all read market data through
``feeds.py``: Yahoo chart JSON (cookie + crumb) primary, Dhan intraday only as
the fallback when Yahoo returns nothing for a symbol. M15 now uses exactly the
same network path — no direct ``yfinance`` calls remain here.

Bar ladder for M15 (per symbol, per interval):

    5m : feeds.fetch_bars_yahoo(interval="5m") -> Dhan 5m -> data/history CSV
    1m : feeds.fetch_bars_yahoo(interval="1m") -> Dhan 1m   (09:15 volume repair)
    1d : committed data/history CSV -> Yahoo daily          (HTF/daily context)

The committed ``data/history/*.csv`` files are the 5-minute history bootstrapped
and refreshed by ``live_runner.py --bootstrap`` and committed by workflow 2 —
the same local-history-first source M2/M5/M6 use for previous-session context.
They are local-only, so daily context costs no network calls and survives a
Yahoo 429 storm.

Dataframe contract (unchanged from the yfinance implementation):
    tz-aware Asia/Kolkata DatetimeIndex, columns
    [Open, High, Low, Close, Volume], sorted ascending, no duplicate stamps.
"""

from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import yaml

from .session import IST, now_ist, session_date

log = logging.getLogger("nse_scanner.data")

ROOT = Path(__file__).resolve().parent.parent
# Flat layout: feeds.py lives at the repo root next to live_runner.py. When the
# package is imported from elsewhere (VPS cron, tests) make that importable.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import feeds  # noqa: E402  (the one shared Yahoo/Dhan feed for every model)

UNIVERSE_PATH = ROOT / "config" / "universe.yaml"
HISTORY_DIR = ROOT / "data" / "history"
FNO_UNIVERSE_PATH = ROOT / "fno_universe.csv"

COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

_INTERVAL_ALIASES = {
    "5": "5m", "5m": "5m", "5min": "5m",
    "1": "1m", "1m": "1m", "1min": "1m",
    "1d": "1d", "d": "1d", "day": "1d", "daily": "1d",
}

_PERIOD_DAYS = {
    "1d": 1, "5d": 5, "7d": 7, "10d": 10, "30d": 30, "60d": 60, "90d": 90,
    "1mo": 30, "3mo": 90, "6mo": 180, "1y": 365, "2y": 730,
}

# Yahoo caps intraday history: 1m <= 7 days, 5m <= 60 days.
_YAHOO_MAX_DAYS = {"1m": 7, "5m": 59}
# Dhan's intraday endpoint is bounded as well.
_DHAN_MAX_DAYS = {"1m": 5, "5m": 60}

_SID_CACHE: dict[str, str] | None = None
_LOCAL_STATS = {"history_calls": 0, "symbols": 0, "sources": {}}
# data/history CSVs do not change during a live session; parsing 20 of them on
# every run_until_ready retry is wasted work. Keyed by (path, mtime, days).
_HIST_CACHE: dict[tuple[str, float, int | None], pd.DataFrame] = {}
_HIST_CACHE_MAX = 160


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS, dtype=float)


# --------------------------------------------------------------------------- #
# Feed audit (mirrors feeds.feed_stats so M15 can persist st["feed"])
# --------------------------------------------------------------------------- #
def reset_feed_stats() -> None:
    """Zero the per-cycle feed counters (called at the start of a scan cycle)."""
    feeds.reset_feed_stats()
    _LOCAL_STATS["history_calls"] = 0
    _LOCAL_STATS["symbols"] = 0
    _LOCAL_STATS["sources"] = {}


def feed_stats() -> dict:
    """One audit record: Yahoo/Dhan counters from feeds + local-history usage."""
    stats = feeds.feed_stats()
    stats.update(
        history_calls=_LOCAL_STATS["history_calls"],
        symbols=_LOCAL_STATS["symbols"],
        sources=dict(_LOCAL_STATS["sources"]),
    )
    return stats


def _note_source(source: str) -> None:
    _LOCAL_STATS["sources"][source] = _LOCAL_STATS["sources"].get(source, 0) + 1


# --------------------------------------------------------------------------- #
# Universe / symbol mapping
# --------------------------------------------------------------------------- #
def load_universe(path: Path | None = None) -> dict:
    with open(path or UNIVERSE_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# TV still uses NSE:LTIM; Yahoo listed the rename LTIM → LTM in 2026.
YAHOO_ALIASES = {
    "LTIM": "LTM.NS",
}


def yahoo_stock(symbol: str) -> str:
    """NSE code -> Yahoo ticker (kept: pipeline keys frames by this value)."""
    s = symbol.strip().upper()
    if s in YAHOO_ALIASES:
        return YAHOO_ALIASES[s]
    if s.endswith(".NS") or s.startswith("^"):
        return s
    return f"{s}.NS"


def plain_symbol(ticker: str) -> str:
    """Yahoo ticker -> NSE code used by fno_universe.csv / data/history/*.csv."""
    s = str(ticker).strip()
    for suffix in (".NS", ".BO"):
        if s.upper().endswith(suffix):
            return s[: -len(suffix)].upper()
    return s.upper()


def load_security_ids() -> dict[str, str]:
    """NSE code -> Dhan securityId (same file M1–M6 use). Cached per process."""
    global _SID_CACHE
    if _SID_CACHE is None:
        out: dict[str, str] = {}
        try:
            with open(FNO_UNIVERSE_PATH, "r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    sym = (row.get("symbol") or "").strip().upper()
                    sid = (row.get("securityId") or "").strip()
                    if sym and sid:
                        out[sym] = sid
        except OSError as e:
            log.warning("fno_universe.csv unavailable (%s) — Dhan fallback disabled", e)
        _SID_CACHE = out
    return _SID_CACHE


def security_id(ticker: str) -> str | None:
    return load_security_ids().get(plain_symbol(ticker))


def history_path(ticker: str) -> Path:
    return HISTORY_DIR / f"{plain_symbol(ticker)}.csv"


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _normalize_ohlcv(df: pd.DataFrame | None) -> pd.DataFrame:
    """Coerce a feeds/Yahoo/CSV frame to the shared capitalized OHLCV contract."""
    if df is None or len(df) == 0:
        return _empty()
    x = df.copy()
    if isinstance(x.columns, pd.MultiIndex):
        x.columns = [c[0] if isinstance(c, tuple) else c for c in x.columns]
    if "dt" in x.columns:
        x = x.set_index("dt")
    cols = {str(c).strip().lower(): c for c in x.columns}
    out = pd.DataFrame(index=x.index)
    for name in COLUMNS:
        key = name.lower()
        if key in cols:
            series = x[cols[key]]
        elif name in x.columns:
            series = x[name]
        else:
            series = 0.0 if name == "Volume" else np.nan
        out[name] = pd.to_numeric(series, errors="coerce")
    idx = out.index
    try:
        if isinstance(idx, pd.DatetimeIndex) and idx.tz is None:
            out.index = idx.tz_localize(IST)
        else:
            out.index = pd.to_datetime(idx, utc=True).tz_convert(IST)
    except (TypeError, ValueError) as e:
        log.warning("dropping frame with unusable timestamps: %s", e)
        return _empty()
    out = out[~out.index.duplicated(keep="last")].sort_index()
    out = out.dropna(subset=["Close"])
    out["Volume"] = out["Volume"].fillna(0.0)
    out.index.name = None
    return out


def _canon_interval(interval: str) -> str:
    iv = _INTERVAL_ALIASES.get(str(interval).strip().lower())
    if iv is None:
        log.warning("unknown interval %r — using 5m", interval)
        return "5m"
    return iv


def _period_days(period: str) -> int:
    key = str(period).strip().lower()
    if key in _PERIOD_DAYS:
        return _PERIOD_DAYS[key]
    if key.endswith("d") and key[:-1].isdigit():
        return max(1, int(key[:-1]))
    if key.endswith("mo") and key[:-2].isdigit():
        return max(1, int(key[:-2]) * 30)
    if key.endswith("y") and key[:-1].isdigit():
        return max(1, int(key[:-1]) * 365)
    return 7


# --------------------------------------------------------------------------- #
# Local committed history (data/history/*.csv, 5-minute bars)
# --------------------------------------------------------------------------- #
def _history_frame(ticker: str, days: int | None = None) -> pd.DataFrame:
    path = history_path(ticker)
    if not path.exists():
        return _empty()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    key = (str(path), mtime, int(days) if days else None)
    cached = _HIST_CACHE.get(key)
    if cached is not None:
        return cached.copy()
    try:
        raw = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.ParserError) as e:
        log.warning("history %s unreadable: %s", path.name, e)
        return _empty()
    _LOCAL_STATS["history_calls"] += 1
    df = _normalize_ohlcv(raw)
    if df.empty:
        return df
    if days:
        cutoff = df.index[-1].normalize() - pd.Timedelta(days=int(days))
        df = df.loc[df.index >= cutoff]
    if len(_HIST_CACHE) >= _HIST_CACHE_MAX:
        _HIST_CACHE.clear()
    _HIST_CACHE[key] = df
    return df.copy()


def _aggregate_daily(df5: pd.DataFrame) -> pd.DataFrame:
    """5-minute CSV bars -> daily OHLCV stamped at 09:15 IST of each session."""
    if df5 is None or df5.empty:
        return _empty()
    x = df5.sort_index().copy()
    x["_sid"] = [session_date(t) for t in x.index]
    g = x.groupby("_sid", sort=True).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    g.index = pd.DatetimeIndex(
        [pd.Timestamp(d).tz_localize(IST) + pd.Timedelta(minutes=9 * 60 + 15) for d in g.index]
    )
    g.index.name = None
    return g.sort_index()


def _history_daily(ticker: str, days: int) -> pd.DataFrame:
    """Daily context from committed history. Empty when the file is missing/stale."""
    h = _history_frame(ticker, days + 15)  # extra sessions for EMA39 warm-up
    if h.empty:
        return _empty()
    age = (now_ist().date() - h.index[-1].date()).days
    if age > 10:
        log.info("history %s is %dd old — falling back to Yahoo daily", ticker, age)
        return _empty()
    d = _aggregate_daily(h)
    if not d.empty and days:
        cutoff = d.index[-1].normalize() - pd.Timedelta(days=int(days))
        d = d.loc[d.index >= cutoff]
    return d


# --------------------------------------------------------------------------- #
# Network legs (both funnel through feeds.py — the M1–M6 path)
# --------------------------------------------------------------------------- #
def _yahoo_bars(ticker: str, interval: str, period: str) -> pd.DataFrame:
    days = _period_days(period)
    cap = _YAHOO_MAX_DAYS.get(interval)
    if cap:
        days = min(days, cap)
    raw = feeds.fetch_bars_yahoo(ticker, f"{days}d", interval=interval)
    return _normalize_ohlcv(raw)


def _dhan_bars(ticker: str, interval: str, period: str) -> pd.DataFrame:
    sid = security_id(ticker)
    if not sid or not feeds.dhan_ok():
        return _empty()
    days = min(_period_days(period), _DHAN_MAX_DAYS.get(interval, 5))
    now = now_ist()
    frm = (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15:00")
    to = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        raw = feeds.fetch_bars_dhan(sid, frm, to, interval="1" if interval == "1m" else "5")
    except Exception as e:  # HTTP 401/429 etc. — one symbol must not kill the cycle
        log.warning("dhan %s %s failed: %s: %s", ticker, interval, type(e).__name__, e)
        return _empty()
    return _normalize_ohlcv(raw)


def _load_bars(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, str]:
    """Fetch one symbol/interval through the M1–M6 feed ladder."""
    iv = _canon_interval(interval)
    days = _period_days(period)

    if iv in ("5m", "1m"):
        df = _yahoo_bars(ticker, iv, period)
        if not df.empty:
            return df, "yahoo"
        df = _dhan_bars(ticker, iv, period)
        if not df.empty:
            return df, "dhan"
        if iv == "5m":
            df = _history_frame(ticker, max(days, 1))
            if not df.empty:
                return df, "history"
        return _empty(), "none"

    # Daily: local committed history first (zero network, like M2/M5/M6),
    # then Yahoo daily (also the only source for Nifty/VIX/sector indices).
    df = _history_daily(ticker, days)
    if not df.empty:
        return df, "history"
    df = _yahoo_bars(ticker, "1d", period)
    if not df.empty:
        return df, "yahoo"
    df = _aggregate_daily(_dhan_bars(ticker, "5m", f"{min(days, 60)}d"))
    if not df.empty:
        return df, "dhan"
    return _empty(), "none"


def fetch_bars(ticker: str, period: str = "7d", interval: str = "5m") -> pd.DataFrame:
    """One symbol's bars (single-shot convenience wrapper)."""
    df, _source = _load_bars(ticker, period, interval)
    return df


def fetch_symbol(ticker: str, period_5m: str = "7d", period_1d: str = "90d") -> dict:
    return {
        "ticker": ticker,
        "m5": fetch_bars(ticker, period_5m, "5m"),
        "d1": fetch_bars(ticker, period_1d, "1d"),
    }


def fetch_many(tickers: Iterable[str], period: str, interval: str) -> dict[str, pd.DataFrame]:
    """Fetch a batch sequentially — the same polite, no-burst pattern as M1–M6.

    A missing symbol yields an empty frame (never raises) so one dead ticker
    cannot abort a sector scan. Local-history hits skip the inter-symbol sleep.
    """
    tickers = list(dict.fromkeys(tickers))
    if not tickers:
        return {}
    try:
        sleep = float(os.environ.get("M15_FEED_SLEEP", os.environ.get("FEED_SLEEP_SECONDS", "0.05")) or 0)
    except ValueError:
        sleep = 0.05
    out: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df, source = _load_bars(ticker, period, interval)
        except Exception as e:  # fail one symbol, never the cycle
            log.warning("feed %s %s failed: %s: %s", ticker, interval, type(e).__name__, e)
            df, source = _empty(), "none"
        out[ticker] = df
        _LOCAL_STATS["symbols"] += 1
        _note_source(source)
        if source not in ("history", "none") and sleep > 0:
            time.sleep(sleep)
    return out


# --------------------------------------------------------------------------- #
# 09:15 volume repair (unchanged behaviour, used by the M15 master engine)
# --------------------------------------------------------------------------- #
def resample_1m_to_5m(df1m: pd.DataFrame) -> pd.DataFrame:
    """Aggregate 1m OHLCV to NSE 5m bars (label = bar open)."""
    if df1m is None or df1m.empty:
        return _empty()
    x = df1m.copy().sort_index()
    if x.index.tz is None:
        x.index = x.index.tz_localize(IST)
    else:
        x.index = x.index.tz_convert(IST)
    out = x.resample("5min", label="left", closed="left").agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    return out.dropna(subset=["Close"])


def patch_zero_volume(df5: pd.DataFrame, df5_from_1m: pd.DataFrame | None) -> pd.DataFrame:
    """Yahoo NSE 5m often stores the 09:15 auction bar with Volume=0.

    Pine/TV has the real opening volume on that bar. RelVol, VWAP, OBV,
    scond08 and the W1 `entry_sell_today` lock all see 0 and misfire
    (false 09:30 EX1 instead of EX8). Copy volume from 1m-aggregated 5m
    onto any 5m bar whose Volume is 0. OHLC stays on the 5m series.
    """
    if df5 is None or df5.empty:
        return df5
    out = df5.copy()
    if df5_from_1m is None or df5_from_1m.empty:
        return out
    src = df5_from_1m
    if src.index.tz is None:
        src = src.copy()
        src.index = src.index.tz_localize(IST)
    else:
        src = src.copy()
        src.index = src.index.tz_convert(IST)
    vol = np.array(out["Volume"].to_numpy(dtype=float), copy=True)
    for i, ts in enumerate(out.index):
        if vol[i] == 0.0 and ts in src.index:
            v = src.at[ts, "Volume"]
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv == fv and fv > 0:
                vol[i] = fv
    out = out.copy()
    out["Volume"] = vol
    return out


def needs_1m_volume(df5: pd.DataFrame | None) -> bool:
    """True only when a 5m frame actually has a zero-volume bar to repair.

    Lets the pipeline skip the (expensive) 1m feed for symbols whose opening
    volume is already published — fewer Yahoo calls, same repaired result.
    """
    if df5 is None or df5.empty or "Volume" not in df5.columns:
        return False
    vol = pd.to_numeric(df5["Volume"], errors="coerce").fillna(0.0)
    return bool((vol == 0.0).any())


def apply_1m_volume(df5: pd.DataFrame, df1m: pd.DataFrame | None) -> pd.DataFrame:
    """Best-effort 09:15 Volume=0 repair. Safe no-op if 1m is missing."""
    if df5 is None or df5.empty or df1m is None or df1m.empty:
        return df5
    try:
        return patch_zero_volume(df5, resample_1m_to_5m(df1m))
    except Exception as e:
        log.warning("1m volume patch skipped: %s", e)
        return df5


def last_session_date(df: pd.DataFrame):
    if df is None or df.empty:
        return None
    return df.index[-1].tz_convert(IST).date()


def sanitize_index_5m(df: pd.DataFrame) -> pd.DataFrame:
    """Drop Yahoo's fake cash-index 5m tail.

    After 15:10 IST Yahoo often repeats a zero-range bar at 15:15/15:20, then
    injects the official 15:30 close as a 15:25 candle. TV's last 5m (and the
    indicator freeze at Open+370m) stays on the last *traded* print (~22317 on
    12 Oct, not 22363).
    """
    if df is None or df.empty:
        return df
    x = df.sort_index()
    minutes = np.array(
        [
            (ts.tz_convert(IST) if getattr(ts, "tzinfo", None) else ts).hour * 60
            + (ts.tz_convert(IST) if getattr(ts, "tzinfo", None) else ts).minute
            for ts in x.index
        ],
        dtype=int,
    )
    rng = (x["High"].to_numpy(dtype=float) - x["Low"].to_numpy(dtype=float))
    dead = (minutes >= 15 * 60 + 15) & (np.abs(rng) < 1e-6)
    x = x.loc[~dead]
    if len(x) >= 2:
        last_ts = x.index[-1]
        t = last_ts.tz_convert(IST) if getattr(last_ts, "tzinfo", None) else last_ts
        if t.hour == 15 and t.minute >= 25:
            last, prev = x.iloc[-1], x.iloc[-2]
            prev_c = float(prev["Close"])
            if abs(float(last["Open"]) - prev_c) <= max(0.05, abs(prev_c) * 1e-4):
                x = x.iloc[:-1]
    return x
