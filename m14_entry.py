"""M14 Ultra High-Conviction A+ Entry Engine — Causal, Deterministic, 1-3 Max/Day.

Mandatory rules:
1. Instrument & Time: Current liquid NSE F&O stock; MIS 5x leverage. Signal time 09:45-11:30 IST.
2. Signal code: Real master scanner variant whitelist only (101, 102, 104, 106, 201, 202, 208, 209, 212, 213, 280). 90/290 blocked.
3. Top OI Spurt Rank: Stock must be in top-10 OI spurts (spurt_rank <= 10).
4. Video Setup: At least one causal video setup active (S1 Morning Base, S3 Flag Breakout, S2 Pivot Pullback).
5. Volume: Same-clock relative volume >= 1.0 (clock_relvol >= 1.0).
6. Market Regime & Breadth Alignment:
   - BUY: Opening market breadth >= 52% (or live breadth >= 50%) and market regime NOT TREND-DOWN / V-REVERSAL bear-trap.
   - SELL: Opening market breadth <= 48% (decline ratio >= 52% or live decline ratio >= 50%) and market regime NOT TREND-UP / V-REVERSAL bull-trap.
7. Anti-Chase & Anti-Crowding:
   - Side-normalized displacement vs previous close in [-1.00%, +1.50%].
   - Sector breadth vs previous close <= 0.45.
8. Candle Quality / CLV: BUY CLV >= 0.60 (close near high); SELL CLV >= 0.60 (close near low).
9. VIX Gate: Prior session VIX return > -2.0%.
10. A+ Score: Composite A+ score >= 70.0 points.

Portfolio controls: 1-3 entries max/day (hard ceiling 3), max 2 concurrent positions, max 1 entry/symbol/day, max 1 entry/sector/day.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Optional, Sequence
import numpy as np
import pandas as pd

MODEL_NAME = "M14 Ultra High-Conviction A+"
ENTRY_START, ENTRY_END = "09:45", "11:30"
MAX_SIGNAL_AGE_MIN = 5.0

OPEN_BREADTH_BUY_MIN = 0.52
OPEN_BREADTH_SELL_MAX = 0.48
LIVE_BREADTH_BUY_MIN = 0.50
LIVE_BREADTH_SELL_MAX = 0.50
PRIOR_VIX_RETURN_MIN = -2.0

DIR_PREV_MIN_PCT = -1.00
DIR_PREV_MAX_PCT = 1.50
SECTOR_BREADTH_MAX = 0.45
MIN_CLOCK_RELVOL = 1.0
MIN_CLV = 0.60
MAX_SPURT_RANK = 10

MAX_TRADES_PER_DAY = 3
MAX_CONCURRENT = 2
MAX_TRADES_PER_SIDE = 3
ONE_TRADE_PER_SYMBOL_DAY = True
ONE_TRADE_PER_SECTOR_DAY = True
MIN_A_PLUS_SCORE = 70.0

ALLOWED_CODES = {
    101: "BUY-EX",
    102: "BUY-EX17",
    104: "BUY-EX5",
    106: "BUY-EX7",
    201: "SELL-EX1",
    202: "SELL-EX2",
    208: "SELL-EX8",
    209: "SELL-EX8",
    212: "SELL-EX12",
    213: "SELL-EX12",
    280: "NORMAL SELL",
}

PREVIEW_CODES = {90, 290}


@dataclass(frozen=True)
class Decision:
    accepted: bool
    score: float
    reason: str
    code: int
    signal: str
    side: str
    time: str
    subtype: str
    features: dict

    def to_dict(self) -> dict:
        return asdict(self)


def _finite(v) -> bool:
    try:
        return bool(np.isfinite(float(v)))
    except (TypeError, ValueError):
        return False


def _clip(v: float, lo: float, hi: float) -> float:
    return float(np.clip(float(v), lo, hi))


def opening_breadth(bars_map: Mapping[str, pd.DataFrame], prev: Mapping[str, float], side: str) -> tuple[float | None, int]:
    """Side-aligned opening breadth across the liquid F&O universe."""
    up = total = 0
    for sym, b in bars_map.items():
        pc = prev.get(sym)
        if b is None or b.empty or not _finite(pc) or float(pc) <= 0:
            continue
        up += int(float(b["open"].iloc[0]) >= float(pc))
        total += 1
    if total < 180:
        return None, total
    bull = up / total
    return (bull if side == "BUY" else 1.0 - bull), total


def causal_features(engine_frame: pd.DataFrame, today_prefix: pd.DataFrame, side: str,
                    prev_close: float, spurt_rank: float | int | None, master_total_score: float,
                    opening_breadth_dir: float | None, live_breadth_dir: float | None,
                    sector_breadth_prev_dir: float | None, prior_vix_return: float,
                    video_setups: Sequence[str] | None, day_regime: str = "MIXED") -> dict:
    """Compute M14 features strictly using data available at the signal bar close."""
    if engine_frame is None or engine_frame.empty or today_prefix is None or today_prefix.empty:
        raise ValueError("missing price bars")
    if not _finite(prev_close) or float(prev_close) <= 0:
        raise ValueError("fresh previous close unavailable")

    f = engine_frame.sort_index()
    c = f["close"].astype(float)
    h = f["high"].astype(float)
    lo = f["low"].astype(float)
    v = f["volume"].astype(float)
    pc = c.shift(1)

    tr = pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)
    atr_series = tr.ewm(alpha=1 / 14, adjust=False, min_periods=5).mean()
    atr = atr_series.iloc[-1]
    if not _finite(atr) or atr <= 0:
        raise ValueError("ATR unavailable")

    e9 = c.ewm(span=9, adjust=False).mean().iloc[-1]
    e20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    e50 = c.ewm(span=50, adjust=False).mean().iloc[-1]

    day = today_prefix
    close = float(day["close"].iloc[-1])
    op = float(day["open"].iloc[-1])
    day_open = float(day["open"].iloc[0])
    hi = float(day["high"].iloc[-1])
    low = float(day["low"].iloc[-1])
    rng = max(hi - low, 1e-6)
    s = 1.0 if side == "BUY" else -1.0

    typ = (day["high"] + day["low"] + day["close"]) / 3.0
    vwap = float((typ * day["volume"]).sum() / max(1.0, float(day["volume"].sum())))

    rel20 = float(v.iloc[-1] / v.tail(20).mean()) if len(v) >= 5 and v.tail(20).mean() > 0 else None

    ts = pd.Timestamp(f.index[-1])
    prior = f[(f.index.strftime("%H:%M") == ts.strftime("%H:%M")) & (f.index.date < ts.date())]["volume"].astype(float).tail(20)
    clock = float(day["volume"].iloc[-1] / prior.median()) if len(prior) >= 2 and prior.median() > 0 else 1.0

    dir_prev = s * (close / float(prev_close) - 1.0) * 100.0
    dir_gap = s * (day_open / float(prev_close) - 1.0) * 100.0

    clv = (close - low) / rng if side == "BUY" else (hi - close) / rng
    setups = sorted(set(video_setups or []))

    sp_rank = float(spurt_rank) if _finite(spurt_rank) else 999.0

    feats = {
        "dir_prev_pct": dir_prev,
        "dir_gap_pct": dir_gap,
        "ema9_20_atr": s * (float(e9) - float(e20)) / float(atr),
        "ema20_50_atr": s * (float(e20) - float(e50)) / float(atr),
        "close_ema20_atr": s * (close - float(e20)) / float(atr),
        "close_vwap_atr": s * (close - float(vwap)) / float(atr),
        "candle_clv": float(clv),
        "body_atr": s * (close - op) / float(atr),
        "range_atr": float(rng / atr),
        "relvol20": rel20,
        "clock_relvol": clock,
        "spurt_rank": sp_rank,
        "master_total_score": float(master_total_score) if _finite(master_total_score) else 0.0,
        "opening_breadth_dir": opening_breadth_dir,
        "live_breadth_dir": live_breadth_dir,
        "sector_breadth_prev_dir": sector_breadth_prev_dir,
        "prior_vix_return": float(prior_vix_return),
        "video_setups": "+".join(setups),
        "video_setup_count": len(setups),
        "s1": int("S1" in setups),
        "s2": int("S2" in setups),
        "s3": int("S3" in setups),
        "s4": int("S4" in setups),
        "day_regime": str(day_regime),
    }
    return feats


def classify_subtype(f: Mapping[str, object]) -> str:
    dp = float(f.get("dir_prev_pct") or 0.0)
    e20 = float(f.get("close_ema20_atr") or 0.0)
    vw = float(f.get("close_vwap_atr") or 0.0)
    if -1.0 <= dp <= 0.20 and (e20 <= 1.32 or vw <= 1.37):
        return "REVERSION-ALIGNMENT"
    if int(f.get("s2") or 0):
        return "PULLBACK-REENTRY"
    if dp >= 2.0 or e20 >= 2.0 or vw >= 2.0:
        return "MOMENTUM-EXTENDED"
    return "MOMENTUM-CONTROLLED"


def score_features(f: Mapping[str, object]) -> float:
    """A+ quality score calculation (0 to 100 points)."""
    cv = float(f.get("clock_relvol")) if _finite(f.get("clock_relvol")) else 1.0
    vol_pts = 25.0 * _clip((cv - 0.8) / 1.2, 0.0, 1.0)

    rank = float(f.get("spurt_rank") or 999.0)
    rank_pts = 20.0 * _clip((11.0 - rank) / 10.0, 0.0, 1.0)

    ms = float(f.get("master_total_score") or 0.0)
    master_pts = 20.0 * _clip((ms - 30.0) / 60.0, 0.0, 1.0)

    cnt = int(f.get("video_setup_count") or 0)
    if int(f.get("s1") or 0) and int(f.get("s3") or 0):
        video_pts = 15.0
    elif int(f.get("s1") or 0):
        video_pts = 12.0
    elif cnt >= 1:
        video_pts = 10.0
    else:
        video_pts = 0.0

    ob = float(f.get("opening_breadth_dir")) if _finite(f.get("opening_breadth_dir")) else 0.5
    breadth_pts = 10.0 * _clip((ob - 0.45) / 0.20, 0.0, 1.0)

    clv = float(f.get("candle_clv") or 0.0)
    body = float(f.get("body_atr") or 0.0)
    exec_pts = 5.0 * _clip((clv - 0.5) / 0.3, 0.0, 1.0) + 5.0 * _clip(body / 0.5, 0.0, 1.0)

    subtype = classify_subtype(f)
    bonus = 5.0 if subtype in ("MOMENTUM-CONTROLLED", "PULLBACK-REENTRY", "REVERSION-ALIGNMENT") else 0.0

    total = vol_pts + rank_pts + master_pts + video_pts + breadth_pts + exec_pts + bonus
    return round(_clip(total, 0.0, 100.0), 2)


def decide(code: int, signal: str, side: str, etime: str, f: Mapping[str, object]) -> Decision:
    """Apply frozen M14 rules. All mandatory gates must pass."""
    code = int(code)
    side = str(side).upper()
    signal = str(signal)
    subtype = classify_subtype(f)

    def no(reason: str) -> Decision:
        return Decision(False, score_features(f), reason, code, signal, side, etime, subtype, dict(f))

    if code in PREVIEW_CODES:
        return no("preview signal code 90/290 is blocked")
    if code not in ALLOWED_CODES:
        return no("signal code not in M14 reliability whitelist")
    if (side == "BUY") != (code < 200):
        return no("side/code mismatch")
    if etime < ENTRY_START or etime > ENTRY_END:
        return no(f"outside {ENTRY_START}-{ENTRY_END} peak morning window")

    rank = float(f.get("spurt_rank") or 999.0)
    if rank > MAX_SPURT_RANK:
        return no(f"spurt rank {rank:.0f} exceeds top-{MAX_SPURT_RANK} requirement")

    video_cnt = int(f.get("video_setup_count") or 0)
    if video_cnt < 1:
        return no("no causal video setup (S1/S2/S3/S4) active at signal bar")

    crv = float(f.get("clock_relvol")) if _finite(f.get("clock_relvol")) else 0.0
    if crv < MIN_CLOCK_RELVOL:
        return no(f"clock relative volume {crv:.2f} below {MIN_CLOCK_RELVOL:.2f}")

    ob = float(f.get("opening_breadth_dir")) if _finite(f.get("opening_breadth_dir")) else None
    if side == "BUY" and _finite(ob) and float(ob) < OPEN_BREADTH_BUY_MIN:
        return no(f"opening market breadth {ob:.3f} below BUY minimum {OPEN_BREADTH_BUY_MIN:.2f}")
    if side == "SELL" and _finite(ob) and float(ob) > OPEN_BREADTH_SELL_MAX:
        return no(f"opening market breadth {ob:.3f} above SELL maximum {OPEN_BREADTH_SELL_MAX:.2f}")

    regime = str(f.get("day_regime") or "MIXED")
    if side == "BUY" and regime in ("TREND-DOWN", "V-REVERSAL / bear-trap"):
        return no(f"BUY blocked in bearish market regime {regime}")
    if side == "SELL" and regime in ("TREND-UP", "V-REVERSAL / bull-trap"):
        return no(f"SELL blocked in bullish market regime {regime}")

    dp = float(f.get("dir_prev_pct") or 0.0)
    if not (DIR_PREV_MIN_PCT <= dp <= DIR_PREV_MAX_PCT):
        return no(f"anti-chase displacement {dp:+.2f}% outside [{DIR_PREV_MIN_PCT:+.2f}%, {DIR_PREV_MAX_PCT:+.2f}%]")

    sb = float(f.get("sector_breadth_prev_dir")) if _finite(f.get("sector_breadth_prev_dir")) else 0.0
    if sb > SECTOR_BREADTH_MAX:
        return no(f"sector is crowded in trade direction ({sb:.3f} > {SECTOR_BREADTH_MAX:.2f})")

    clv = float(f.get("candle_clv") or 0.0)
    if clv < MIN_CLV:
        return no(f"candle CLV {clv:.2f} below minimum {MIN_CLV:.2f}")

    vix_ret = float(f.get("prior_vix_return") or 0.0)
    if vix_ret <= PRIOR_VIX_RETURN_MIN:
        return no(f"prior VIX return {vix_ret:+.2f}% <= {PRIOR_VIX_RETURN_MIN:+.2f}%")

    score = score_features(f)
    if score < MIN_A_PLUS_SCORE:
        return no(f"A+ score {score:.2f} below floor {MIN_A_PLUS_SCORE:.2f}")

    return Decision(True, score, "qualified", code, signal, side, etime, subtype, dict(f))
