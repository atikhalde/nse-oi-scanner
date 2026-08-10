"""M12 Selective Reversion entry model — causal, deterministic, max five/day.

This module is independent of every existing model. It consumes a real master-scanner
chart signal and information known at that signal bar's close. It never reads outcomes,
end-of-day breadth, future candles, or report-derived labels during live decisions.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Optional

import numpy as np
import pandas as pd

# Current master_scanner.py code map (do not use learn_log.code_of for SELL codes;
# that helper misses the SELL-EX5S offset and is wrong from SELL-EX6 onward).
ALLOWED_CODES = {
    102: "BUY-EX17",
    201: "SELL-EX1",
    202: "SELL-EX2",
    209: "SELL-EX8",
    213: "SELL-EX12",
    216: "SELL-EX15",
    220: "SELL-EX19",
    280: "NORMAL SELL",
}

MODEL_NAME = "M12 Selective Reversion"
MAX_TRADES_PER_DAY = 5
MAX_TRADES_PER_SIDE = 3
ONE_TRADE_PER_SYMBOL_DAY = True
ONE_TRADE_PER_SECTOR_DAY = True

# Frozen research thresholds. Values are side-normalized: positive means extended in
# the proposed trade direction. M12 explicitly rejects directional chasing.
DIR_PREV_MIN_PCT = -1.00
DIR_PREV_MAX_PCT = 0.20
EMA9_20_BONUS_ATR = 0.17
EMA20_50_BONUS_ATR = -0.40
CLOSE_EMA20_BONUS_ATR = 1.32
# Cross-model interaction result: A+ entries occur while the stock's sector is
# counter/neutral, not already crowded in the proposed direction.
SECTOR_BREADTH_MAX = 0.43
MIN_VIDEO_SETUPS = 1
MIN_CLOCK_RELVOL = 1.0
DIR_GAP_BONUS_MAX_PCT = -0.18
ENTRY_START = "09:45"
ENTRY_END = "12:00"
# A missed/queued workflow must not backfill a paper entry at an old signal close.
MAX_SIGNAL_AGE_MIN = 5.0


@dataclass(frozen=True)
class Decision:
    accepted: bool
    score: float
    reason: str
    code: int
    signal: str
    side: str
    time: str
    dir_prev_pct: Optional[float] = None
    ema9_20_atr: Optional[float] = None
    ema20_50_atr: Optional[float] = None
    close_ema20_atr: Optional[float] = None
    dir_gap_pct: Optional[float] = None
    sector_breadth_prev_dir: Optional[float] = None
    video_setup_count: Optional[float] = None
    video_setups: Optional[str] = None
    clock_relvol: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _finite(v) -> bool:
    try:
        return bool(np.isfinite(float(v)))
    except (TypeError, ValueError):
        return False


def causal_price_features(engine_frame: pd.DataFrame, today_prefix: pd.DataFrame,
                          side: str, prev_close: float,
                          sector_breadth_prev_dir: float | None = None,
                          video_setups: list[str] | tuple[str, ...] | None = None) -> dict:
    """Compute M12 features using bars no later than the signal-bar close.

    engine_frame: prior history plus today's prefix, indexed by timestamp.
    today_prefix: today's bars through the signal bar (columns open/high/low/close).
    prev_close: actual immediately preceding trading session close.
    """
    if engine_frame is None or engine_frame.empty or today_prefix is None or today_prefix.empty:
        raise ValueError("missing price bars")
    if not _finite(prev_close) or float(prev_close) <= 0:
        raise ValueError("fresh previous close unavailable")
    f = engine_frame.sort_index()
    c = f["close"].astype(float)
    h = f["high"].astype(float)
    lo = f["low"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - lo), (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().iloc[-1]
    e9 = c.ewm(span=9, adjust=False).mean().iloc[-1]
    e20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]
    if not _finite(atr) or float(atr) <= 0:
        raise ValueError("ATR unavailable")
    s = 1.0 if side == "BUY" else -1.0
    close = float(today_prefix["close"].iloc[-1])
    day_open = float(today_prefix["open"].iloc[0])
    setups = list(video_setups or [])
    # Current bar volume versus the median of the preceding 20 sessions at the
    # same clock time. This avoids comparing 09:45 volume with midday volume.
    ts = pd.Timestamp(f.index[-1])
    prior_same_clock = f[(f.index.strftime("%H:%M") == ts.strftime("%H:%M")) &
                         (f.index.date < ts.date())]["volume"].astype(float).tail(20)
    clock_relvol = (float(today_prefix["volume"].iloc[-1]) / float(prior_same_clock.median())
                    if len(prior_same_clock) >= 5 and float(prior_same_clock.median()) > 0 else None)
    return {
        "dir_prev_pct": s * (close / float(prev_close) - 1.0) * 100.0,
        "ema9_20_atr": s * (float(e9) - float(e20)) / float(atr),
        "ema20_50_atr": s * (float(e20) - float(e50)) / float(atr),
        "close_ema20_atr": s * (close - float(e20)) / float(atr),
        "dir_gap_pct": s * (day_open / float(prev_close) - 1.0) * 100.0,
        "sector_breadth_prev_dir": (float(sector_breadth_prev_dir)
                                     if _finite(sector_breadth_prev_dir) else None),
        "video_setup_count": len(setups),
        "video_setups": "+".join(sorted(setups)),
        "clock_relvol": clock_relvol,
    }


def decide(code: int, signal: str, side: str, etime: str,
           features: Mapping[str, float | None]) -> Decision:
    """Apply the frozen M12 rules. All hard gates precede score calculation."""
    code = int(code)
    signal = str(signal)
    side = str(side).upper()
    base = dict(code=code, signal=signal, side=side, time=etime)
    vals = {k: features.get(k) for k in (
        "dir_prev_pct", "ema9_20_atr", "ema20_50_atr", "close_ema20_atr",
        "dir_gap_pct", "sector_breadth_prev_dir", "video_setup_count", "video_setups",
        "clock_relvol")}

    def no(reason: str) -> Decision:
        return Decision(False, 0.0, reason, **base, **vals)

    if code not in ALLOWED_CODES:
        return no("signal family not in frozen reliability whitelist")
    if signal and signal != ALLOWED_CODES[code]:
        return no(f"code/name mismatch: {code} is {ALLOWED_CODES[code]}, received {signal}")
    if side not in {"BUY", "SELL"}:
        return no("invalid side")
    if (side == "BUY") != (code < 200):
        return no("side/code mismatch")
    if etime < ENTRY_START or etime > ENTRY_END:
        return no(f"outside {ENTRY_START}-{ENTRY_END} entry window")
    for k in ("dir_prev_pct", "sector_breadth_prev_dir", "video_setup_count", "clock_relvol"):
        if not _finite(vals[k]):
            return no(f"required causal feature unavailable: {k}")

    dp = float(vals["dir_prev_pct"])
    sb = float(vals["sector_breadth_prev_dir"])
    videos = int(float(vals["video_setup_count"]))
    if not (DIR_PREV_MIN_PCT <= dp <= DIR_PREV_MAX_PCT):
        return no(f"anti-chase displacement failed ({dp:+.3f}% not in "
                  f"[{DIR_PREV_MIN_PCT:+.2f}%, {DIR_PREV_MAX_PCT:+.2f}%])")
    if sb > SECTOR_BREADTH_MAX:
        return no(f"sector is crowded in signal direction ({sb:.3f} > {SECTOR_BREADTH_MAX:.2f})")
    if videos < MIN_VIDEO_SETUPS:
        return no("no causal video setup aligned at the signal bar")
    crv = float(vals["clock_relvol"])
    if crv < MIN_CLOCK_RELVOL:
        return no(f"same-time relative volume too low ({crv:.2f} < {MIN_CLOCK_RELVOL:.2f})")

    e920 = float(vals["ema9_20_atr"]) if _finite(vals["ema9_20_atr"]) else np.inf
    e2050 = float(vals["ema20_50_atr"]) if _finite(vals["ema20_50_atr"]) else np.inf

    # Transparent quality score, used only to rank simultaneous qualified signals.
    points = 70.0
    points += 3.0 if dp <= 0.0 else (2.0 if dp <= 0.10 else 1.0)
    if e920 <= EMA9_20_BONUS_ATR:
        points += 2.0
    if e2050 <= EMA20_50_BONUS_ATR:
        points += 2.0
    if _finite(vals["close_ema20_atr"]) and float(vals["close_ema20_atr"]) <= CLOSE_EMA20_BONUS_ATR:
        points += 1.0
    if sb <= SECTOR_BREADTH_MAX:
        points += 1.0
    points += min(2.0, float(videos))
    if _finite(vals["dir_gap_pct"]) and float(vals["dir_gap_pct"]) <= DIR_GAP_BONUS_MAX_PCT:
        points += 1.0
    return Decision(True, points, "qualified", **base, **vals)


def sector_breadth_at(bars_map: Mapping[str, pd.DataFrame],
                      prev_closes: Mapping[str, float],
                      sector_of: Mapping[str, str], sector: str, side: str,
                      at_timestamp) -> float | None:
    """Side-aligned sector breadth vs previous close at one historical timestamp.

    The timestamp filter is essential during catch-up cycles: using each feed's newest
    bar for an older signal would leak future information.
    """
    up = total = 0
    ts = pd.Timestamp(at_timestamp)
    for sym, bars in bars_map.items():
        if sector_of.get(sym) != sector or bars is None or bars.empty:
            continue
        pc = prev_closes.get(sym)
        if not _finite(pc) or float(pc) <= 0:
            continue
        b = bars[pd.to_datetime(bars["dt"]) <= ts]
        if b.empty:
            continue
        up += int(float(b["close"].iloc[-1]) >= float(pc))
        total += 1
    if total < 3:
        return None
    bull = up / total
    return bull if side == "BUY" else 1.0 - bull


def market_sector_context_at(bars_map: Mapping[str, pd.DataFrame],
                             prev_closes: Mapping[str, float],
                             sector_of: Mapping[str, str], candidate_sym: str,
                             sector: str, side: str, at_timestamp) -> dict:
    """Causal breadth and leading/lagging telemetry at the signal timestamp.

    This reconstructs F&O-member breadth/returns. It is intentionally labelled as a
    proxy, not the unavailable official M6/M8 sector-index/VIX persistence board.
    """
    ts = pd.Timestamp(at_timestamp); moves = []; sec_moves = []; stock_move = None
    for sym, bars in bars_map.items():
        pc = prev_closes.get(sym)
        if bars is None or bars.empty or not _finite(pc) or float(pc) <= 0:
            continue
        b = bars[pd.to_datetime(bars["dt"]) <= ts]
        if b.empty:
            continue
        move = (float(b["close"].iloc[-1]) / float(pc) - 1.0) * 100.0
        moves.append(move)
        if sector_of.get(sym) == sector:
            sec_moves.append(move)
        if sym == candidate_sym:
            stock_move = move
    if not moves:
        return {}
    s = 1.0 if side == "BUY" else -1.0
    market_adv = float(np.mean(np.asarray(moves) >= 0))
    market_med = float(np.median(moves))
    out = {
        "market_adv_ratio": market_adv,
        "market_breadth_dir": market_adv if s > 0 else 1.0 - market_adv,
        "dir_market_pct": s * market_med,
        "market_regime": "BULL" if market_adv >= .55 else ("BEAR" if market_adv <= .45 else "NEUTRAL"),
    }
    if sec_moves:
        sec_adv = float(np.mean(np.asarray(sec_moves) >= 0)); sec_med = float(np.median(sec_moves))
        out.update(sector_adv_ratio=sec_adv,
                   sector_breadth_prev_dir=sec_adv if s > 0 else 1.0-sec_adv,
                   dir_sector_pct=s*sec_med,
                   sector_leads_market=bool(s*sec_med >= s*market_med),
                   sector_lags_market=bool(s*sec_med < s*market_med))
        if stock_move is not None:
            out.update(dir_stock_pct=s*stock_move,
                       stock_leads_sector=bool(s*stock_move >= s*sec_med))
    return out
