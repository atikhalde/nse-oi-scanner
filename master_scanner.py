# -*- coding: utf-8 -*-
"""
==================================================================================
MASTER SECTOR BATCH SCANNER  -  Python port of the TradingView Pine Script v6
"MASTER SECTOR BATCH SCANNER" (Intraday Strength Meter v6.3)
==================================================================================

This is a line-by-line faithful conversion.  It keeps:

  * the exact bar-by-bar execution ORDER of the original script (all `var`
    state variables, daily resets and `if` blocks are updated at the same
    point of the bar as in Pine),
  * Pine `na` semantics (mapped to NaN - boolean tests on NaN are False,
    exactly like Pine's na in conditions),
  * request.security(..., gaps_off, lookahead_on) behaviour ON HISTORICAL
    bars: the daily / 15-min values are the FINAL values of the HTF bar the
    5-min bar belongs to (this is what TradingView renders on history),
  * all outputs that matter for entries:
        entry_buy_today / entry_sell_today                          (base)
        normalEntry + exceptionBuyEntry ... exception13BuyEntry     (BUY)
        snormalEntry + sexceptionSellEntry(Ex1) + sellException2-19 (SELL)
        bullish5mBreakout / bearish5mBreakout, ORB breaks,
        the scanner exception code per bar (f_scannerExceptionCode).

Data requirements (to obtain a 100% match with the chart):
  * 5-minute bars of the symbol, regular session, timestamps = bar OPEN time
    in the exchange timezone (Asia/Kolkata for NSE symbols),
  * enough warm-up history (the TV scanner uses calc_bars_count=1500),
  * columns: open, high, low, close, volume  (index = DatetimeIndex).

Author: converted for user, 2026.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

NAN = float("nan")


# ==============================================================================
# 1. PINE-EXACT PRIMITIVES   (ta.* equivalents with Pine seeding / na behaviour)
# ==============================================================================

def pine_ema(src: np.ndarray, length: int) -> np.ndarray:
    """ta.ema : alpha = 2/(length+1), seeded with the first non-na value."""
    alpha = 2.0 / (length + 1.0)
    out = np.full(len(src), NAN)
    prev = NAN
    for i, x in enumerate(src):
        if math.isnan(prev):
            prev = x                 # stays na while src is na (Pine: na(sum[1]) ? src)
        else:
            prev = x if math.isnan(x) else alpha * x + (1.0 - alpha) * prev
        out[i] = prev
    return out


def pine_rma(src: np.ndarray, length: int) -> np.ndarray:
    """ta.rma : alpha = 1/length, seeded with the first non-na value."""
    alpha = 1.0 / float(length)
    out = np.full(len(src), NAN)
    prev = NAN
    for i, x in enumerate(src):
        if math.isnan(prev):
            prev = x
        else:
            prev = x if math.isnan(x) else alpha * x + (1.0 - alpha) * prev
        out[i] = prev
    return out


def pine_sma(src: np.ndarray, length: int) -> np.ndarray:
    """ta.sma : na until `length` non-na values are available."""
    s = pd.Series(src)
    return s.rolling(length, min_periods=length).mean().to_numpy()


def pine_highest(src: np.ndarray, length: int) -> np.ndarray:
    s = pd.Series(src)
    return s.rolling(length, min_periods=length).max().to_numpy()


def pine_lowest(src: np.ndarray, length: int) -> np.ndarray:
    s = pd.Series(src)
    return s.rolling(length, min_periods=length).min().to_numpy()


def pine_linreg_end(src: np.ndarray, length: int) -> np.ndarray:
    """ta.linreg(src, length, 0) - least-squares fitted value at the window end.
    na if any na inside the window."""
    n = len(src)
    out = np.full(n, NAN)
    if n < length:
        return out
    xs = np.arange(length, dtype=float)
    sx = xs.sum()
    sxx = (xs * xs).sum()
    den = length * sxx - sx * sx
    for i in range(length - 1, n):
        w = src[i - length + 1: i + 1]
        if np.isnan(w).any():
            continue
        sxy = float((xs * w).sum())
        sy = float(w.sum())
        slope = (length * sxy - sx * sy) / den
        intercept = (sy - slope * sx) / length
        out[i] = intercept + slope * (length - 1)      # offset = 0
    return out


def pine_cum(src: np.ndarray) -> np.ndarray:
    """ta.cum : Pine propagates na (nz(prev)+src)."""
    out = np.full(len(src), NAN)
    prev = NAN
    for i, x in enumerate(src):
        if math.isnan(x):
            prev = NAN
        else:
            prev = x if math.isnan(prev) else prev + x
        out[i] = prev
    return out


def pine_rsi(src: np.ndarray, length: int) -> np.ndarray:
    """ta.rsi - Wilder RMA version used by Pine."""
    n = len(src)
    ch = np.full(n, NAN)
    ch[1:] = src[1:] - src[:-1]
    up_raw = np.where(ch > 0, ch, np.where(np.isnan(ch), NAN, 0.0))
    dn_raw = np.where(ch < 0, -ch, np.where(np.isnan(ch), NAN, 0.0))
    up = pine_rma(up_raw, length)
    dn = pine_rma(dn_raw, length)
    out = np.full(n, NAN)
    for i in range(n):
        u, d = up[i], dn[i]
        if math.isnan(u) or math.isnan(d):
            continue
        if d == 0:
            out[i] = 100.0
        elif u == 0:
            out[i] = 0.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + u / d)
    return out


def pine_macd(src: np.ndarray, fast: int, slow: int, sig: int):
    """ta.macd -> (macd_line, signal_line, hist)."""
    line = pine_ema(src, fast) - pine_ema(src, slow)
    signal = pine_ema(line, sig)
    return line, signal, line - signal


def anchored_vwap(price: np.ndarray, volume: np.ndarray,
                  new_anchor: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Session-anchored VWAP.  Returns (vwap_result_with_nan_on_zero_vol,
    raw_cum_pv, raw_cum_vol).  Pine's ta.vwap -> na while cum volume == 0."""
    n = len(price)
    pv = price * volume
    cum_pv = np.zeros(n)
    cum_vol = np.zeros(n)
    run_pv = 0.0
    run_vol = 0.0
    out = np.full(n, NAN)
    for i in range(n):
        if new_anchor[i]:
            run_pv, run_vol = pv[i], volume[i]
        else:
            run_pv += pv[i]
            run_vol += volume[i]
        cum_pv[i], cum_vol[i] = run_pv, run_vol
        out[i] = run_pv / run_vol if run_vol != 0 else NAN
    return out, cum_pv, cum_vol


# ==============================================================================
# 2. PARAMETERS  (all input.* of the script, with the same defaults)
# ==============================================================================

@dataclass
class Params:
    # strengths
    len_ema1: int = 20
    len_ema2: int = 50
    rsi_length: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_sig: int = 9
    strong_thresh: int = 60

    # consolidation / breakout
    cons_len_5m: int = 7
    cons_atr_mult: float = 1.2
    use_vwap_filter: bool = True
    min_rel_vol_buy: float = 0.2
    min_rel_vol_sell: float = 0.2
    gap_threshold_pct: float = 1.5
    sl_perc: float = 0.5 / 100.0

    # first/second/third base windows (BUY and SELL use the same hours)
    buy_windows: Tuple[Tuple[int, int, int, int], ...] = ((9, 16, 9, 59),
                                                          (10, 0, 11, 44),
                                                          (11, 45, 15, 15))
    sell_windows: Tuple[Tuple[int, int, int, int], ...] = ((9, 16, 9, 59),
                                                           (10, 0, 11, 44),
                                                           (11, 45, 15, 15))
    # PDH/PDL breakout ignore window
    ignore_window: Tuple[int, int, int, int] = (9, 0, 9, 35)

    # master BUY entry window (inputs)  -  09:26 -> 11:15
    fbuy_start: Tuple[int, int] = (9, 26)
    fbuy_end: Tuple[int, int] = (11, 15)
    # master SELL entry window (inputs) -  09:26 -> 12:00
    fsell_start: Tuple[int, int] = (9, 26)
    fsell_end: Tuple[int, int] = (12, 0)

    # ORB
    orb_start: Tuple[int, int] = (9, 15)
    orb_end: Tuple[int, int] = (9, 30)

    # ---- exception toggles (defaults mirror the Pine script) ----
    enable_sell_normal: bool = True
    enable_sell_ex1: bool = True
    enable_sell_ex2: bool = True
    enable_sell_ex3: bool = True
    enable_sell_ex4: bool = True
    enable_sell_ex5: bool = True
    enable_sell_ex5s: bool = True
    enable_sell_ex6: bool = True
    enable_sell_ex7: bool = True
    enable_sell_ex8: bool = True
    enable_sell_ex9: bool = True
    enable_sell_ex10: bool = True
    enable_sell_ex11: bool = True
    enable_sell_ex12: bool = True
    enable_sell_ex13: bool = True
    enable_sell_ex14: bool = True
    enable_sell_ex15: bool = True
    enable_sell_ex16: bool = True
    enable_sell_ex17: bool = True
    enable_sell_ex18: bool = True
    enable_sell_ex19: bool = True

    enable_buy_normal: bool = True
    enable_buy_ex: bool = True
    enable_buy_ex17: bool = True
    enable_buy_ex4: bool = True
    enable_buy_ex5: bool = True
    enable_buy_ex6: bool = True
    enable_buy_ex7: bool = True
    enable_buy_ex8: bool = True
    enable_buy_ex9: bool = True
    enable_buy_ex10: bool = True
    enable_buy_ex11: bool = True
    enable_buy_ex12: bool = True
    enable_buy_ex13: bool = True

    # scanner
    sector: str = "AUTO"
    batch: str = "Batch 1"
    scanner_calc_bars: int = 1500


def _in_window(h: int, m: int, sh: int, sm: int, eh: int, em: int) -> bool:
    """Exact replica of the Pine time-window boolean."""
    return ((h > sh) or (h == sh and m >= sm)) and ((h < eh) or (h == eh and m <= em))


def _b(x) -> bool:
    """Pine boolean coercion of possibly-nan values in conditions."""
    try:
        if x is None:
            return False
        if isinstance(x, float) and math.isnan(x):
            return False
        return bool(x)
    except (TypeError, ValueError):
        return False


# ==============================================================================
# 3. INPUT PREPARATION  (all bar-by-bar independent series, vectorised)
# ==============================================================================

class _Inputs:
    """Container for every series that the state machine reads."""

    def __init__(self, df: pd.DataFrame, p: Params,
                 earnings_dates: Optional[set] = None):
        df = df.sort_index()
        self.df = df
        self.p = p
        idx = df.index
        # exchange-local time of each bar (bar OPEN time) - hour()/minute(tf5_time)
        self.times = idx
        self.win_h = idx.hour.to_numpy()
        self.win_m = idx.minute.to_numpy()
        self.dates = pd.Series(idx.date, index=idx)

        o = df["open"].to_numpy(float)
        h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float)
        c = df["close"].to_numpy(float)
        v = df["volume"].to_numpy(float)
        self.o, self.h, self.l, self.c, self.v = o, h, l, c, v
        n = len(df)
        self.n = n

        # --- session flags ---------------------------------------------------
        # isNewDay = isNewSession = isFirst5MinBar = ta.change(time("D")) != 0
        # On the very first dataset bar ta.change is na -> False ( Pine-faithful )
        date_arr = np.array(idx.date)
        day_change = np.zeros(n, dtype=bool)
        day_change[1:] = date_arr[1:] != date_arr[:-1]
        self.day_change = day_change                     # isNewDay / isFirst5MinBar / isNewSession
        # isNew5MinBar = tf5_time != tf5_time[1]  (timestamps unique -> every bar after 0)
        self.new5min = np.zeros(n, dtype=bool)
        self.new5min[1:] = idx.values[1:] != idx.values[:-1]

        # --- chart EMAs / MACD / RSI ----------------------------------------
        self.ema20 = pine_ema(c, p.len_ema1)
        self.ema50 = pine_ema(c, p.len_ema2)
        self.rsi = pine_rsi(c, p.rsi_length)
        macd, signal, hist = pine_macd(c, p.macd_fast, p.macd_slow, p.macd_sig)
        self.macd, self.signal, self.hist = macd, signal, hist

        # --- manual session VWAP (vwap_val) ---------------------------------
        typ = (h + l + c) / 3.0
        # request ta.vwap equivalents:
        #   vwap (ta.vwap(close))  / tf5_vwap_val (ta.vwap(tf5_close))
        vwap_close, _, _ = anchored_vwap(c, v, day_change)
        self.vwap = vwap_close
        self.tf5_vwap_val = vwap_close
        #   vwap5 = ta.vwap(hlc3)
        vwap_hlc3, _, _ = anchored_vwap(typ, v, day_change)
        self.vwap5 = vwap_hlc3
        #   manual vwap_val (typ price with fallback to close on zero volume)
        vwap_val_man, cum_pv, cum_vol = anchored_vwap(typ, v, day_change)
        self.vwap_val = np.where(cum_vol != 0.0, vwap_val_man, c)

        # --- TR / ATR14 / +DM / -DM / DX / ADX5 (ta.rma family) --------------
        tr = np.full(n, NAN)
        hl = h - l
        hc = np.abs(h - np.roll(c, 1))
        lc = np.abs(l - np.roll(c, 1))
        hc[0] = NAN
        lc[0] = NAN                # math.max with na -> na -> tr na on bar 0
        tr[1:] = np.maximum(hl[1:], np.maximum(hc[1:], lc[1:]))
        self.tf5_tr = tr
        self.atr14 = pine_rma(tr, 14)                 # tf5_atr14 == atr5 (ta.atr(14))
        up_move = np.full(n, NAN)
        dn_move = np.full(n, NAN)
        up_move[1:] = h[1:] - h[:-1]
        dn_move[1:] = l[:-1] - l[1:]
        plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move,
                           np.where(np.isnan(up_move), NAN, 0.0))
        minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move,
                            np.where(np.isnan(dn_move), NAN, 0.0))
        sm_plus = pine_rma(plus_dm, 14)
        sm_minus = pine_rma(minus_dm, 14)
        with np.errstate(all="ignore"):
            plus_di = np.where(self.atr14 != 0, 100.0 * sm_plus / self.atr14, 0.0)
            minus_di = np.where(self.atr14 != 0, 100.0 * sm_minus / self.atr14, 0.0)
            di_sum = plus_di + minus_di
            dx = np.where(di_sum != 0,
                          100.0 * np.abs(plus_di - minus_di) / di_sum, 0.0)
        dx = np.where(np.isnan(self.atr14), NAN, dx)  # rma seeds only when real data exists
        self.adx5 = pine_rma(dx, 14)

        # --- OBV manual + slope + relative volume -----------------------------
        obv = np.zeros(n)
        for i in range(1, n):
            if c[i] > c[i - 1]:
                obv[i] = obv[i - 1] + v[i]
            elif c[i] < c[i - 1]:
                obv[i] = obv[i - 1] - v[i]
            else:
                obv[i] = obv[i - 1]
        self.obv = obv
        self.obv_slope = pine_sma(obv, 5) - pine_sma(obv, 20)
        self.rel_vol = v / pine_sma(v, 20)

        avg_vol5 = pine_sma(v, 20)                     # tf5_avgVol
        self.tf5_rel_vol = np.where(avg_vol5 > 0, v / avg_vol5, 0.0)

        # --- 5-min OBV (ta.cum - na propagating) + linreg slope ---------------
        change = np.full(n, NAN)
        change[1:] = c[1:] - c[:-1]
        self.tf5_obv = pine_cum(change * v)
        self.tf5_obv_slope = pine_linreg_end(self.tf5_obv, 20)

        # --- consolidation / wicks / range ------------------------------------
        self.cons_high_prev = np.roll(pine_highest(h, p.cons_len_5m), 1)
        self.cons_high_prev[0] = NAN
        self.cons_low_prev = np.roll(pine_lowest(l, p.cons_len_5m), 1)
        self.cons_low_prev[0] = NAN
        cons_high = pine_highest(h, p.cons_len_5m)
        cons_low = pine_lowest(l, p.cons_len_5m)
        width = cons_high - cons_low
        self.is_cons_now = width <= self.atr14 * p.cons_atr_mult
        self.is_cons_prev = np.roll(self.is_cons_now, 1)
        self.is_cons_prev[0] = False

        rng = h - l
        up_wick = h - np.maximum(o, c)
        lo_wick = np.minimum(o, c) - l
        self.upper_wick_pct = np.where(rng > 0, up_wick / rng * 100.0, 0.0)
        self.lower_wick_pct = np.where(rng > 0, lo_wick / rng * 100.0, 0.0)

        hh20_prev = np.roll(pine_highest(h, 20), 1)
        hh20_prev[0] = NAN
        ll20_prev = np.roll(pine_lowest(l, 20), 1)
        ll20_prev[0] = NAN
        self.hh20_prev, self.ll20_prev = hh20_prev, ll20_prev
        self.hh7_prev = np.roll(pine_highest(h, 7), 1)
        self.hh7_prev[0] = NAN
        self.ll7_prev = np.roll(pine_lowest(l, 7), 1)
        self.ll7_prev[0] = NAN
        self.range_break = _bsafe(h > hh20_prev) | _bsafe(l < ll20_prev)

        # --- DISTANCE FROM VWAP / body flags ----------------------------------
        with np.errstate(all="ignore"):
            self.dist_low = np.abs(l - self.vwap) / self.vwap * 100.0
            self.dist_high = np.abs(h - self.vwap) / self.vwap * 100.0
        buy_filter = _bsafe(self.dist_low <= 1.0)
        sell_filter = _bsafe(self.dist_high <= 1.0)
        self.final_buy_filter = buy_filter if p.use_vwap_filter else np.ones(n, bool)
        self.final_sell_filter = sell_filter if p.use_vwap_filter else np.ones(n, bool)

        body_high = np.maximum(o, c)
        body_low = np.minimum(o, c)
        self.body_above_vwap = _bsafe(body_low > self.vwap_val)      # (only kept for parity)
        self.body_mostly_above = _bsafe(c > self.vwap)
        self.body_below_vwap = _bsafe(body_high < self.vwap_val)     # (parity)
        self.body_mostly_below = _bsafe(c < self.vwap)
        self.long_allowed = _bsafe(c > self.vwap)
        self.short_allowed = _bsafe(c < self.vwap)

        # --- strength (totalScore) --------------------------------------------
        trend_base = np.where((c > self.ema20) & (self.ema20 > self.ema50), 20,
                              np.where(c > self.ema20, 10, 0))
        trend_vwap = np.where(_bsafe(c > self.vwap_val), 10, 0)
        trend_score = trend_base + trend_vwap
        mom_rsi = np.where(self.rsi > 55, 12, np.where(self.rsi > 50, 6, 0))
        mom_macd = np.where(self.hist > 0, 13, 0)
        momentum_score = mom_rsi + mom_macd
        adx_score = np.where(self.adx5 >= 25, 15,
                             np.where(self.adx5 >= 20, 10,
                                      np.where(self.adx5 >= 15, 5, 0)))
        volume_score = np.where((self.obv_slope > 0) & (self.rel_vol > 1.5), 15,
                                np.where(self.obv_slope > 0, 10, 0))
        stock_ret = np.full(n, NAN)
        with np.errstate(all="ignore"):
            prev_c = np.roll(c, 1)
            prev_c[0] = NAN
            stock_ret = np.where(prev_c != 0, (c - prev_c) / prev_c, 0.0)
        rs_score = np.where(stock_ret > 0, 15, 0)
        self.total_score = trend_score + momentum_score + adx_score + volume_score + rs_score
        self.is_strong = self.total_score >= p.strong_thresh

        # ------------------------------------------------------------------
        # request.security("D", ...) with gaps_off + lookahead_on (historical)
        # ------------------------------------------------------------------
        day = pd.DataFrame({"d": date_arr, "o": o, "h": h, "l": l, "c": c})
        g = day.groupby("d", sort=True)
        day_ohlc = pd.DataFrame({"dOpen": g["o"].first(), "dHigh": g["h"].max(),
                                 "dLow": g["l"].min(), "dClose": g["c"].last()})
        day_ohlc["prevClose"] = day_ohlc["dClose"].shift(1)
        day_ohlc["prevHigh"] = day_ohlc["dHigh"].shift(1)
        day_ohlc["prevLow"] = day_ohlc["dLow"].shift(1)
        day_map = day_ohlc.reindex(date_arr)
        self.dOpen = day_map["dOpen"].to_numpy(float)
        self.dHigh = day_map["dHigh"].to_numpy(float)
        self.dLow = day_map["dLow"].to_numpy(float)
        self.dClose = day_map["dClose"].to_numpy(float)            # full-day close (lookahead)
        self.prev_day_close = day_map["prevClose"].to_numpy(float)
        self.prev_day_high = day_map["prevHigh"].to_numpy(float)
        self.prev_day_low = day_map["prevLow"].to_numpy(float)

        # daily pivots
        daily_pivot = (self.dHigh + self.dLow + self.dClose) / 3.0
        self.dailyR2 = 2.0 * daily_pivot - self.dLow
        self.dailyS2 = 2.0 * daily_pivot - self.dHigh
        correct_pivot = (self.prev_day_high + self.prev_day_low + self.prev_day_close) / 3.0
        self.correct_pivot = correct_pivot
        self.PVOTR1 = 2 * correct_pivot - self.prev_day_low
        self.PVOTR2 = correct_pivot + (self.prev_day_high - self.prev_day_low)
        self.PVOTR3 = self.prev_day_high + 2 * (correct_pivot - self.prev_day_low)
        self.PVOTS1 = 2 * correct_pivot - self.prev_day_high
        self.PVOTS2 = correct_pivot - (self.prev_day_high - self.prev_day_low)
        self.PVOTS3 = self.prev_day_low - 2 * (self.prev_day_high - correct_pivot)

        # ------------------------------------------------------------------
        # request.security("15", [ema20, ema50, ta.vwap, close, open]) + lookahead
        # ------------------------------------------------------------------
        if n > 0:
            floored = idx.floor("15min")
            m5 = pd.DataFrame({"k": floored, "d": date_arr,
                               "o": o, "h": h, "l": l, "c": c, "v": v})
            g15 = m5.groupby("k", sort=True)
            q = pd.DataFrame({"open15": g15["o"].first(), "high15": g15["h"].max(),
                              "low15": g15["l"].min(), "close15": g15["c"].last(),
                              "vol15": g15["v"].sum(), "d15": g15["d"].first()})
            close15 = q["close15"].to_numpy(float)
            ema20_15 = pine_ema(close15, 20)
            ema50_15 = pine_ema(close15, 50)
            dchange15 = np.zeros(len(q), dtype=bool)
            darr15 = q["d15"].to_numpy()
            dchange15[1:] = darr15[1:] != darr15[:-1]
            hlc3_15 = ((q["high15"] + q["low15"] + q["close15"]) / 3.0).to_numpy(float)
            vwap15, _, _ = anchored_vwap(hlc3_15, q["vol15"].to_numpy(float), dchange15)
            q["ema20_15"], q["ema50_15"], q["vwap_15"] = ema20_15, ema50_15, vwap15
            mapped = q.reindex(floored)        # gaps_off: forward-fill if a 15m slot is empty
            mapped = mapped.ffill()
            self._15 = mapped["close15"].to_numpy(float)
            self._15_open = mapped["open15"].to_numpy(float)
            self.vwap_15 = mapped["vwap_15"].to_numpy(float)
            self.ema20_15 = mapped["ema20_15"].to_numpy(float)
            self.ema50_15 = mapped["ema50_15"].to_numpy(float)
        else:
            self._15 = self._15_open = self.vwap_15 = self.ema20_15 = self.ema50_15 = np.array([])

        self.trend15m_up = _bsafe(self.ema20_15 > self.ema50_15)
        self.trend15m_down = _bsafe(self.ema20_15 < self.ema50_15)

        # EMA39 computed ON THE CHART from the mapped series (exactly like the Pine)
        self.ema39_D = pine_ema(self.dClose, 39)
        self.ema39_15 = pine_ema(self._15, 39)

        # --- misc price refs ---------------------------------------------------
        self.c1 = np.roll(c, 1); self.c1[0] = NAN
        self.c2 = np.roll(c, 2); self.c2[:2] = NAN
        self.h1 = np.roll(h, 1); self.h1[0] = NAN
        self.h2 = np.roll(h, 2); self.h2[:2] = NAN
        self.l1 = np.roll(l, 1); self.l1[0] = NAN
        self.l2 = np.roll(l, 2); self.l2[:2] = NAN

        self.rsi5 = pine_rsi(c, 14)
        self.smaatr5 = pine_sma(self.atr14, 20)
        self.atr5 = self.atr14                 # same formula, kept as separate name like Pine
        self.ema20_5 = self.ema20

        # BUY original breakout level / SELL original breakdown level
        self.b_resistance = np.maximum(self.dHigh, hh20_prev)
        self.s_support = np.minimum(self.dLow, ll20_prev)
        self.b_original_breakout = _bsafe(c > self.b_resistance)
        self.s_original_breakdown = _bsafe(c < self.s_support)

        # earnings day highlight (visual only).  Pass earnings_dates=set(date,...)
        if earnings_dates is not None:
            self.is_earnings_day = np.array([d in earnings_dates for d in date_arr])
        else:
            self.is_earnings_day = np.zeros(n, dtype=bool)

        # ORB session membership (time(timeframe.period,'0915-0930:1234567'))
        tmin = self.win_h * 60 + self.win_m
        os_min = p.orb_start[0] * 60 + p.orb_start[1]
        oe_min = p.orb_end[0] * 60 + p.orb_end[1]
        self.orb_in_session = (tmin >= os_min) & (tmin < oe_min)


def _bsafe(arr):
    """elementwise Pine-bool coercion of a possibly-nan float/bool array."""
    arr = np.asarray(arr)
    if arr.dtype == bool:
        return arr
    return np.where(np.isnan(arr.astype(float)), False, arr.astype(bool))


# ==============================================================================
# 4. PER-BAR STATE MACHINE  (faithful transcription of the Pine script order)
# ==============================================================================

SCAN_CODE_NAME = {
    80: "NORMAL BUY", 90: "ENTRY BUY",
    101: "BUY-EX", 102: "BUY-EX17", 103: "BUY-EX4", 104: "BUY-EX5",
    105: "BUY-EX6", 106: "BUY-EX7", 107: "BUY-EX8", 108: "BUY-EX9",
    109: "BUY-EX10", 110: "BUY-EX11", 111: "BUY-EX12", 112: "BUY-EX13",
    201: "SELL-EX1", 202: "SELL-EX2", 203: "SELL-EX3", 204: "SELL-EX4",
    205: "SELL-EX5", 206: "SELL-EX5S", 207: "SELL-EX6", 208: "SELL-EX7",
    209: "SELL-EX8", 210: "SELL-EX9", 211: "SELL-EX10", 212: "SELL-EX11",
    213: "SELL-EX12", 214: "SELL-EX13", 215: "SELL-EX14", 216: "SELL-EX15",
    217: "SELL-EX16", 218: "SELL-EX17", 219: "SELL-EX18", 220: "SELL-EX19",
    280: "NORMAL SELL", 290: "ENTRY SELL",
}


def run_symbol(df: pd.DataFrame, params: Optional[Params] = None,
               earnings_dates: Optional[set] = None) -> pd.DataFrame:
    """Run the complete indicator on one symbol's 5-minute OHLCV DataFrame.

    Returns a DataFrame aligned with the input index containing every signal
    and debug column.  Entry columns are booleans.
    """
    p = params or Params()
    I = _Inputs(df, p, earnings_dates)
    n = I.n
    o, h, l, c, v = I.o, I.h, I.l, I.c, I.v

    R: Dict[str, np.ndarray] = {}


    def col(name, dtype=float):
        arr = np.full(n, NAN) if dtype is float else np.zeros(n, dtype=dtype)
        R[name] = arr
        return arr

    # base entries & breakouts
    r_entry_buy = col("entry_buy_today", bool)
    r_entry_sell = col("entry_sell_today", bool)
    r_entry_buy_px = col("entry_buy_price")
    r_entry_buy_sl = col("entry_buy_stop")
    r_entry_sell_px = col("entry_sell_price")
    r_entry_sell_sl = col("entry_sell_stop")
    r_bull5 = col("bullish5mBreakout", bool)
    r_bear5 = col("bearish5mBreakout", bool)
    r_orb_bull = col("orb_bull_break", bool)
    r_orb_bear = col("orb_bear_break", bool)

    # BUY master entries
    r_b_normal = col("buy_normalEntry", bool)
    r_b_ex = col("buy_exceptionEntry", bool)             # BUY-EX
    r_b_ex17 = col("buy_newExceptionEntry", bool)        # BUY-EX17
    r_b_ex4 = col("buy_exception4", bool)
    r_b_ex5 = col("buy_exception5", bool)
    r_b_ex6 = col("buy_exception6", bool)
    r_b_ex7 = col("buy_exception7", bool)
    r_b_ex8 = col("buy_exception8", bool)
    r_b_ex9 = col("buy_exception9", bool)
    r_b_ex10 = col("buy_exception10", bool)
    r_b_ex11 = col("buy_exception11", bool)
    r_b_ex12 = col("buy_exception12", bool)
    r_b_ex13 = col("buy_exception13", bool)
    r_b_any = col("allActive_new", bool)                 # any BUY fired

    # SELL master entries
    r_s_normal = col("sell_normalEntry", bool)
    r_s_ex1 = col("sell_exception1", bool)               # sexceptionSellEntry
    r_s_ex2 = col("sell_exception2", bool)
    r_s_ex3 = col("sell_exception3", bool)
    r_s_ex4 = col("sell_exception4", bool)
    r_s_ex5 = col("sell_exception5", bool)
    r_s_ex5s = col("sell_exception5s", bool)
    r_s_ex6 = col("sell_exception6", bool)
    r_s_ex7 = col("sell_exception7", bool)
    r_s_ex8 = col("sell_exception8", bool)
    r_s_ex9 = col("sell_exception9", bool)
    r_s_ex10 = col("sell_exception10", bool)
    r_s_ex11 = col("sell_exception11", bool)
    r_s_ex12 = col("sell_exception12", bool)
    r_s_ex13 = col("sell_exception13", bool)
    r_s_ex14 = col("sell_exception14", bool)
    r_s_ex15 = col("sell_exception15", bool)
    r_s_ex16 = col("sell_exception16", bool)
    r_s_ex17 = col("sell_exception17", bool)
    r_s_ex18 = col("sell_exception18", bool)
    r_s_ex19 = col("sell_exception19", bool)
    r_s_any = col("actualSellEntry", bool)

    # misc / debug mirrors of the Pine tables
    r_total = col("totalScore")
    r_base = col("baseActive")
    r_sbase = col("sbaseActive")
    r_cond22 = col("cond22", bool)
    r_scond22 = col("scond22", bool)
    r_firstsn = col("firstStrongSignal", bool)
    r_firstgw = col("firstGreenAfterWeak", bool)
    r_firstwk = col("firstWeakSignal", bool)
    r_scan = col("scan_code", int)
    r_failed_b = col("buy_fail_count", int)
    r_failed_s = col("sell_fail_count", int)
    r_orb_hi = col("finalOrbHigh")
    r_orb_lo = col("finalOrbLow")

    # ---- var state ------------------------------------------------------
    wasStrong = False
    buySignalWindow1 = buySignalWindow2 = buySignalWindow3 = False
    sellSignalWindow1 = sellSignalWindow2 = sellSignalWindow3 = False
    firstWindowBigGap = False
    gapUpOccurred = False
    gapDownOccurred = False
    bullish5mTriggeredToday = False
    bearish5mTriggeredToday = False

    orb_high = NAN
    orb_low = NAN
    finalOrbHigh = NAN
    finalOrbLow = NAN
    orbBullFired = False
    orbBearFired = False

    # BUY state
    first5mOpen_new = NAN
    vwap15_at_first5m = NAN
    prevDayClose_from5m = NAN
    first5mHigh_new = NAN
    first5mLow_new = NAN
    maxFirst6Size_new = 0.0
    barsInSession_new = 0
    priceAt915_new = NAN
    priceAt945_new = NAN
    b1_breakoutLevel = 0.0
    b1_breakoutOccurred = False
    b1_retestCompleted = False
    b1_barsSinceBreakout = 0
    b2_structureLevel = 0.0
    b2_recentHigh = 0.0
    b2_barsSinceHigh = 0
    b2_structureFormed = False
    b2_breakoutLevel = 0.0
    b2_breakoutOccurred = False
    b2_retestCompleted = False
    b2_barsSinceBreakout = 0
    buyWindowFired = False
    first5mLow_check = 0.0
    bcandlesBelowVwap = 0
    bstopTrackingCandles = False
    bmaxCandleSizeUntilEntry = 0.0
    todayHigh = h[0] if n else NAN   # var init low[0], then max(...,h[0]) -> h[0] on bar 0
    todayHigh_prev = NAN

    # SELL state
    sfirst5mHigh_new = NAN
    sfirst5mLow_new = NAN
    sfirst5mFullSize = NAN
    sfirst5mBodySize = NAN
    smaxFirst6Size_new = 0.0
    smaxCandleSizeUntilEntry = 0.0
    sstopTrackingCandles = False
    scandlesAboveVwap = 0
    shasFullCandleAboveVwap = False
    spriceAt915_new = NAN
    spriceAt945_new = NAN
    s1_breakdownLevel = 0.0
    s1_breakdownOccurred = False
    s1_retestCompleted = False
    s1_barsSinceBreakdown = 0
    s2_structureLevel = 0.0
    s2_recentLow = 0.0
    s2_barsSinceLow = 0
    s2_structureFormed = False
    s2_breakdownLevel = 0.0
    s2_breakdownOccurred = False
    s2_retestCompleted = False
    s2_barsSinceBreakdown = 0
    sellWindowFired = False
    first5mHigh_check = 0.0
    todayLow = l[0] if n else NAN    # var init high[0], then min(...,l[0]) -> l[0] on bar 0
    todayLow_prev = NAN
    smaxCandleRangeUntilEntry = 0.0
    ssumCandleRangeUntilEntry = 0.0
    scandleCountUntilEntry = 0

    (bsh, bsm), (beh, bem) = p.fbuy_start, p.fbuy_end
    (ssh, ssm), (seh, sem) = p.fsell_start, p.fsell_end
    bw1, bw2, bw3 = p.buy_windows
    sw1, sw2, sw3 = p.sell_windows
    iws, iwm, iwe, iwem = p.ignore_window

    for i in range(n):
        oi, hi, li, ci, vi = o[i], h[i], l[i], c[i], v[i]
        hh, mm = int(I.win_h[i]), int(I.win_m[i])
        isNewDay = bool(I.day_change[i])            # isNewDay == isNewSession == isFirst5MinBar
        isFirst5MinBar = isNewDay
        isNew5MinBar = bool(I.new5min[i])
        wh, wm = hh, mm                              # win_h/win_m

        c0 = ci
        c1 = I.c1[i]; c2 = I.c2[i]
        h0 = hi; h1 = I.h1[i]; h2 = I.h2[i]
        l0 = li; l1 = I.l1[i]; l2 = I.l2[i]

        # ---------- FIRST-TIME STRONG/WEAK TRACKER ----------
        prevWasStrong = False if isNewDay else wasStrong
        isStrong = bool(I.is_strong[i])
        firstStrongSignal = isStrong and not prevWasStrong
        firstWeakSignal = (not isStrong) and prevWasStrong and I.total_score[i] < 55
        firstRedAfterStrong = prevWasStrong and not isStrong and I.total_score[i] < 55
        firstGreenAfterWeak = (not prevWasStrong) and isStrong and I.total_score[i] >= p.strong_thresh
        wasStrong = isStrong
        r_firstsn[i] = firstStrongSignal
        r_firstgw[i] = firstGreenAfterWeak
        r_firstwk[i] = firstWeakSignal

        # ---------- GAP FLAGS (once per day, on first bar) ----------
        pdc = I.prev_day_close[i]
        if isFirst5MinBar:
            g1 = _b((oi - pdc) / pdc > p.gap_threshold_pct / 100.0) if pdc and not math.isnan(pdc) else False
            g2 = _b((pdc - oi) / pdc > p.gap_threshold_pct / 100.0) if pdc and not math.isnan(pdc) else False
            firstWindowBigGap = g1 or g2
            gapDownOccurred = g2
            gapUpOccurred = g1

        # ==================================================================
        # BASE 5-MIN BREAKOUT ENTRIES (entry_buy_today / entry_sell_today)
        # ==================================================================
        inBuyWindow = _in_window(wh, wm, *bw1)
        inSecBuyWindow = _in_window(wh, wm, *bw2)
        inThiBuyWindow = _in_window(wh, wm, *bw3)
        inSellWindow = _in_window(wh, wm, *sw1)
        inSecSellWindow = _in_window(wh, wm, *sw2)
        inThiSellWindow = _in_window(wh, wm, *sw3)

        liquidityBuy = _b(I.tf5_rel_vol[i] >= p.min_rel_vol_buy)
        liquiditySell = _b(I.tf5_rel_vol[i] >= p.min_rel_vol_sell)
        rangeBreak = bool(I.range_break[i])

        agg_entry_buy = ((((inBuyWindow or inSecBuyWindow or inThiBuyWindow) and
                           _b(hi > I.cons_high_prev[i])) or
                          (firstGreenAfterWeak or firstStrongSignal)) and
                         bool(I.final_buy_filter[i]) and
                         (I.upper_wick_pct[i] <= 25 or I.body_mostly_above[i]) and
                         bool(I.long_allowed[i]) and rangeBreak)
        # -- Per-window signal locks: daily reset (Pine: if isNewDay -> all false) --
        if isNewDay:
            buySignalWindow1 = False
            buySignalWindow2 = False
            buySignalWindow3 = False
        entry_buy = agg_entry_buy and I.total_score[i] > 60
        inWindow1, inWindow2, inWindow3 = inBuyWindow, inSecBuyWindow, inThiBuyWindow
        entry_buy_today = (((entry_buy and inWindow1 and not buySignalWindow1 and
                             not firstWindowBigGap and not gapUpOccurred) or
                            (entry_buy and inWindow2 and not buySignalWindow2) or
                            (entry_buy and inWindow3 and not buySignalWindow3)) and
                           liquidityBuy)
        if entry_buy_today:
            if inWindow1:
                buySignalWindow1 = True
            elif inWindow2:
                buySignalWindow2 = True
            elif inWindow3:
                buySignalWindow3 = True
        r_entry_buy[i] = entry_buy_today
        if entry_buy_today:
            r_entry_buy_px[i] = ci
            r_entry_buy_sl[i] = li * (1.0 - p.sl_perc)

        sell_agg = ((((inSellWindow or inSecSellWindow or inThiSellWindow) and
                      _b(li < I.cons_low_prev[i])) or
                     (firstWeakSignal or firstRedAfterStrong)) and
                    bool(I.final_sell_filter[i]) and
                    (I.lower_wick_pct[i] <= 25 or I.body_mostly_below[i]) and
                    bool(I.short_allowed[i]) and rangeBreak)
        # -- SELL per-window signal locks: daily reset --
        if isNewDay:
            sellSignalWindow1 = False
            sellSignalWindow2 = False
            sellSignalWindow3 = False
        entry_sell = sell_agg and I.total_score[i] < 40
        inSWindow1, inSWindow2, inSWindow3 = inSellWindow, inSecSellWindow, inThiSellWindow
        entry_sell_today = (((entry_sell and inSWindow1 and not sellSignalWindow1 and
                              not firstWindowBigGap and not gapDownOccurred) or
                             (entry_sell and inSWindow2 and not sellSignalWindow2) or
                             (entry_sell and inSWindow3 and not sellSignalWindow3)) and
                            liquiditySell)
        if entry_sell_today:
            if inSWindow1:
                sellSignalWindow1 = True
            elif inSWindow2:
                sellSignalWindow2 = True
            elif inSWindow3:
                sellSignalWindow3 = True
        r_entry_sell[i] = entry_sell_today
        if entry_sell_today:
            r_entry_sell_px[i] = ci
            r_entry_sell_sl[i] = hi * (1.0 + p.sl_perc)

        # ==================================================================
        # PREVIOUS DAY HIGH/LOW 5-MIN BREAKOUTS
        # ==================================================================
        ignoreWindow = _in_window(wh, wm, iws, iwm, iwe, iwem)
        if isNewDay:
            bullish5mTriggeredToday = False
            bearish5mTriggeredToday = False
        bullish5mBreakout = _b(ci > I.prev_day_high[i]) and not bullish5mTriggeredToday and \
            not ignoreWindow and rangeBreak and bool(I.long_allowed[i])
        if bullish5mBreakout:
            bullish5mTriggeredToday = True
        bearish5mBreakout = _b(ci < I.prev_day_low[i]) and not bearish5mTriggeredToday and \
            not ignoreWindow and rangeBreak and bool(I.short_allowed[i])
        if bearish5mBreakout:
            bearish5mTriggeredToday = True
        r_bull5[i] = bullish5mBreakout
        r_bear5[i] = bearish5mBreakout

        # ==================================================================
        # ORB (09:15-09:30) - session tracker
        # ==================================================================
        in_session = bool(I.orb_in_session[i])
        # Pine: is_first = in_session and not in_session[1]  (na on dataset bar 0 -> False)
        is_first = in_session and (i > 0 and not I.orb_in_session[i - 1])
        if i == 0:
            orb_high_prev = NAN                        # orb_high[1] on bar 0 is na
            orb_low_prev = NAN
        if is_first:
            orb_high, orb_low = hi, li
        else:
            orb_high, orb_low = orb_high_prev, orb_low_prev
        if _b(hi > orb_high) and in_session:
            orb_high = hi
        if _b(li < orb_low) and in_session:
            orb_low = li
        orbSessionJustEnded = (i > 0 and I.orb_in_session[i - 1]) and not in_session
        if orbSessionJustEnded:
            finalOrbHigh = orb_high_prev
            finalOrbLow = orb_low_prev
        if (not in_session) and math.isnan(finalOrbHigh) and not math.isnan(orb_high):
            finalOrbHigh = orb_high
            finalOrbLow = orb_low
        if isNewDay:
            orbBullFired = False
            orbBearFired = False
            finalOrbHigh = NAN
            finalOrbLow = NAN
        orbValid = not (math.isnan(finalOrbHigh) or math.isnan(finalOrbLow))
        c1v = I.c1[i]
        orbBullBreak = orbValid and not orbBullFired and _b(ci > finalOrbHigh) and _b(c1v <= finalOrbHigh)
        orbBearBreak = orbValid and not orbBearFired and _b(ci < finalOrbLow) and _b(c1v >= finalOrbLow)
        if orbBullBreak:
            orbBullFired = True
        if orbBearBreak:
            orbBearFired = True
        r_orb_bull[i] = orbBullBreak
        r_orb_bear[i] = orbBearBreak
        orb_high_prev = orb_high
        orb_low_prev = orb_low
        r_orb_hi[i] = finalOrbHigh
        r_orb_lo[i] = finalOrbLow

        # ==================================================================
        # BUY SIDE - MASTER ENTRIES (normal + 13 exceptions)
        # ==================================================================
        # -- first 5-min bar capture + vwapnotfar --
        if isFirst5MinBar:
            first5mOpen_new = oi
            vwap15_at_first5m = I.vwap_15[i]
            prevDayClose_from5m = c1                             # tf5_close[1]
        gapPct_vs_prevClose = NAN
        if not (math.isnan(prevDayClose_from5m) or prevDayClose_from5m == 0 or
                math.isnan(first5mOpen_new)):
            gapPct_vs_prevClose = (first5mOpen_new - prevDayClose_from5m) / prevDayClose_from5m * 100
        isGapSmall = (not math.isnan(gapPct_vs_prevClose)) and gapPct_vs_prevClose < 0.75
        vwap15_dist = NAN
        if not (math.isnan(first5mOpen_new) or first5mOpen_new == 0 or math.isnan(vwap15_at_first5m)):
            vwap15_dist = (vwap15_at_first5m - first5mOpen_new) / first5mOpen_new * 100
        vwapnotfar = True if isGapSmall else (True if math.isnan(vwap15_dist) else vwap15_dist <= 0.5)

        isCloseAbovePivot_ex4 = _b(ci > I.correct_pivot[i])
        belowPP = _b(ci < I.correct_pivot[i])
        abovePP = _b(ci > I.correct_pivot[i])
        aboves3 = _b(ci > I.PVOTS3[i])
        belowPDH = _b(ci < I.prev_day_high[i])
        closeAboveVWAP = _b(ci > I.vwap_15[i])

        # -- first 5-min candle size including gap (BUY copy) --
        if isFirst5MinBar:
            first5mHigh_new = hi
            first5mLow_new = li
        first5mRange = max(first5mHigh_new, pdc) - min(first5mLow_new, pdc) if not math.isnan(pdc) else NAN
        first5mSizePct_new = first5mRange / pdc * 100.0 if (not math.isnan(first5mRange) and pdc != 0) else NAN

        # -- first 6 candles max size (ignore first) --
        if isFirst5MinBar:
            barsInSession_new = 0
            maxFirst6Size_new = 0.0
        if isNew5MinBar and not isFirst5MinBar:
            barsInSession_new += 1
            if 2 <= barsInSession_new <= 6:
                candleSize_new = (ci - oi) / ci * 100.0
                maxFirst6Size_new = max(maxFirst6Size_new, candleSize_new)

        # -- rejection candle block (BUY - computed but unused downstream, kept for parity) --
        bodySize = abs(ci - oi)
        totalRange = hi - li
        upperShadow = hi - max(oi, ci)
        lowerShadow = min(oi, ci) - li
        validRange = totalRange > 0.001
        isRejectionFromHighs = validRange and (upperShadow > lowerShadow * 3) and \
            (upperShadow > totalRange * 0.25) and (ci < hi - upperShadow * 0.5)
        notBearishHammer = not isRejectionFromHighs

        # -- first 30min change (kept for parity) --
        if isFirst5MinBar:
            priceAt915_new = ci
            priceAt945_new = NAN
        if barsInSession_new == 6 and math.isnan(priceAt945_new):
            priceAt945_new = ci

        # -- BUY retest machine 1 (resistance breakout) --
        b1_resistanceLevel = I.b_resistance[i]
        if (not b1_breakoutOccurred) and _b(ci > b1_resistanceLevel) and not isFirst5MinBar:
            b1_breakoutOccurred = True
            b1_breakoutLevel = b1_resistanceLevel
            b1_barsSinceBreakout = 0
        if b1_breakoutOccurred:
            b1_barsSinceBreakout += 1
            tol = 0.003
            b1_isRetest = _b(li >= b1_breakoutLevel * (1 - tol)) and \
                _b(li <= b1_breakoutLevel * (1 + tol)) and _b(ci > b1_breakoutLevel)
            if b1_isRetest:
                b1_retestCompleted = True
            if b1_barsSinceBreakout > 15:
                b1_breakoutOccurred = False

        # -- BUY BOS/CHoCH retest machine 2 --
        b2_isSwingHigh = all(_b(hi > x) for x in (h1, h2,
                                                  I.h[i - 3] if i >= 3 else NAN,
                                                  I.h[i - 4] if i >= 4 else NAN,
                                                  I.h[i - 5] if i >= 5 else NAN,
                                                  I.h[i - 6] if i >= 6 else NAN))
        if b2_isSwingHigh and not b2_structureFormed:
            b2_recentHigh = hi
            b2_barsSinceHigh = 0
            b2_structureLevel = l1
            b2_structureFormed = True
        if b2_structureFormed:
            b2_barsSinceHigh += 1
            b2_lowerHighFormed = _b(hi < b2_recentHigh) and b2_barsSinceHigh >= 3
            if b2_lowerHighFormed and _b(ci > b2_structureLevel) and not b2_breakoutOccurred:
                b2_breakoutOccurred = True
                b2_breakoutLevel = b2_structureLevel
                b2_barsSinceBreakout = 0
                b2_retestCompleted = False
            if _b(ci > b2_recentHigh):
                b2_structureFormed = False
        if b2_breakoutOccurred:
            b2_barsSinceBreakout += 1
            tol = 0.003
            b2_isRetest = _b(li >= b2_breakoutLevel * (1 - tol)) and \
                _b(li <= b2_breakoutLevel * (1 + tol)) and _b(ci > b2_breakoutLevel)
            if b2_isRetest:
                b2_retestCompleted = True
            if b2_barsSinceBreakout > 15:
                b2_breakoutOccurred = False

        # -- BUY condition stack (01-21 base + 22 break/retest) --
        cond01 = 1 if _b(I.rsi5[i] > (I.rsi5[i - 1] if i >= 1 else NAN)) else 0
        cond02 = 1 if _b(I.rsi5[i] > 55) else 0
        cond03 = 1 if _b(c0 > I.vwap5[i]) else 0
        cond04 = 1 if _b(c0 > c1) else 0
        cond05 = 1 if _b(I.atr5[i] > 0.5 * I.smaatr5[i]) else 0
        cond06 = 1 if _b(c0 > I.ema20_5[i]) else 0
        cond07 = 1 if _b(c0 > oi) else 0
        cond08 = 1 if _b(vi * c0 > 5000000) else 0
        cond09 = 1 if _b(c0 > h1) else 0
        cond10 = 1 if _b(I.vwap5[i] > (I.vwap5[i - 1] if i >= 1 else NAN)) else 0
        cond11 = 1 if _b(c0 > h2) else 0
        cond12 = 1 if _b((h0 - l0) / c0 < 0.015) else 0
        cond13 = 1 if _b(I._15[i] > I._15_open[i]) else 0
        cond14 = 1 if _b(ci > oi) else 0
        cond15 = 1 if _b(I.dOpen[i] > 50) else 0
        cond16 = 1 if _b(I.dClose[i] > I.ema39_D[i]) else 0
        cond17 = 1 if _b(I._15[i] > I.ema39_15[i]) else 0
        cond18 = 1 if _b(h0 > I.hh7_prev[i]) else 0
        cond19 = 1 if _b(first5mSizePct_new < 4) else 0
        cond20 = 1 if _b(maxFirst6Size_new < 1.5) else 0
        cond21 = 1 if _b(ci < I.dailyR2[i]) else 0

        b_retestDone = (b1_breakoutOccurred and b1_retestCompleted) or \
                       (b2_breakoutOccurred and b2_retestCompleted)
        b_before1015 = (wh < 10) or (wh == 10 and wm < 11)
        cond22 = 1 if (b_retestDone if b_before1015 else
                       (bool(I.b_original_breakout[i]) or b_retestDone)) else 0
        r_cond22[i] = bool(cond22)

        baseActive = (cond01 + cond02 + cond03 + cond04 + cond05 + cond06 + cond07 +
                      cond08 + cond09 + cond10 + cond11 + cond12 + cond13 + cond14 +
                      cond15 + cond16 + cond17 + cond18 + cond19 + cond20 + cond21)
        failedConditions = 21 - baseActive
        totalFailedConditions = failedConditions + (1 if cond22 == 0 else 0)
        exceptionEntryAlt = baseActive >= 20
        useExceptionEntry = exceptionEntryAlt
        normalTriggerScore = (baseActive + cond22) if b_before1015 else \
            (22 if baseActive >= 21 else
             (22 if (baseActive >= 20 and b_retestDone) else baseActive + cond22))
        exceptionTriggerScore = 22 if baseActive >= 21 else baseActive + 1
        triggerScore = exceptionTriggerScore if useExceptionEntry else normalTriggerScore

        # -- BUY entry window + daily reset block --
        finBuyWindow = _in_window(wh, wm, bsh, bsm, beh, bem)
        if isNewDay:
            buyWindowFired = False
            first5mLow_check = 0.0
            b1_breakoutOccurred = False
            b1_retestCompleted = False
            b1_breakoutLevel = 0.0
            b1_barsSinceBreakout = 0
            b2_structureLevel = 0.0
            b2_recentHigh = 0.0
            b2_barsSinceHigh = 0
            b2_structureFormed = False
            b2_breakoutOccurred = False
            b2_retestCompleted = False
            b2_breakoutLevel = 0.0
            b2_barsSinceBreakout = 0
        if isFirst5MinBar:
            first5mLow_check = li
        first5mIsDayLow_new = _b(first5mLow_check == I.dLow[i])

        # -- candles below 15m VWAP (never stops on BUY side - Pine-faithful) --
        if isFirst5MinBar:
            bcandlesBelowVwap = 0
            bstopTrackingCandles = False
        if isNew5MinBar and not isFirst5MinBar and not bstopTrackingCandles:
            if _b(ci < I.vwap_15[i]):
                bcandlesBelowVwap += 1
        if isNewDay:
            bcandlesBelowVwap = 0
            bstopTrackingCandles = False
        isVwapBullish = bcandlesBelowVwap < 4

        if isFirst5MinBar:
            bmaxCandleSizeUntilEntry = 0.0
        if isNew5MinBar and not isFirst5MinBar and not bstopTrackingCandles:
            bcandleSizeUntilEntry = abs((ci - oi) / ci * 100.0)
            bmaxCandleSizeUntilEntry = max(bmaxCandleSizeUntilEntry, bcandleSizeUntilEntry)
        if isNewDay:
            bmaxCandleSizeUntilEntry = 0.0
        bmaxCandleSizeOk = bmaxCandleSizeUntilEntry < 0.5

        # -- today-high tracking --
        s_brokePreDayHigh = _b(ci > I.prev_day_high[i])
        if i > 0:
            todayHigh_prev_end = todayHigh                      # todayHigh[1]
        else:
            todayHigh_prev_end = NAN
        if isNewDay:
            todayHigh = hi
        else:
            todayHigh = hi if math.isnan(todayHigh) else max(todayHigh, hi)
        if i == 0:
            todayHigh = hi
        isNewDayHigh = _b(hi >= todayHigh_prev_end)
        isCloseAboveDayHigh = _b(ci >= todayHigh_prev_end)

        # -- normal + exception BUY entries --
        normalEntry = (triggerScore >= 22) and finBuyWindow and not buyWindowFired and \
            p.enable_buy_normal and first5mIsDayLow_new and not useExceptionEntry

        exceptionBuyEntry = useExceptionEntry and (baseActive >= 20) and isNewDayHigh and \
            s_brokePreDayHigh and finBuyWindow and not buyWindowFired and p.enable_buy_ex and \
            (s_brokePreDayHigh or entry_buy_today) and vwapnotfar

        only17and28Failed = baseActive == 21 and cond22 == 0
        newExceptionBuyEntry = only17and28Failed and isNewDayHigh and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex17 and \
            (s_brokePreDayHigh or entry_buy_today) and isCloseAboveDayHigh and isVwapBullish

        first5mNotDayLow = not first5mIsDayLow_new
        only8_11_17_Failed = baseActive == 21
        exception4BuyEntry = only8_11_17_Failed and first5mNotDayLow and isCloseAboveDayHigh and \
            isCloseAbovePivot_ex4 and finBuyWindow and not buyWindowFired and p.enable_buy_ex4 and \
            (s_brokePreDayHigh or entry_buy_today)

        only8and28Failed = baseActive == 21 and cond22 == 0
        exception5BuyEntry = only8and28Failed and isNewDayHigh and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex5 and isCloseAboveDayHigh and \
            (s_brokePreDayHigh or entry_buy_today)

        # (script order: EX7 defined before EX6)
        only8and17Failed = baseActive == 21
        exception7BuyEntry = only8and17Failed and isNewDayHigh and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex7 and (s_brokePreDayHigh or entry_buy_today)

        baseActiveExcept8_28_ex6 = baseActive          # identical formula in the script
        first5mNotDayLow_ex6 = not first5mIsDayLow_new
        only8_28_andFirst5Failed_ex6 = baseActiveExcept8_28_ex6 >= 21 and cond22 == 0 and \
            first5mNotDayLow_ex6
        exception6BuyEntry = only8_28_andFirst5Failed_ex6 and isNewDayHigh and \
            isCloseAboveDayHigh and finBuyWindow and not buyWindowFired and p.enable_buy_ex6 and \
            isCloseAbovePivot_ex4

        only28Failed = baseActive == 21 and cond22 == 0
        exception8BuyEntry = only28Failed and isNewDayHigh and isCloseAboveDayHigh and \
            finBuyWindow and not buyWindowFired and p.enable_buy_ex8 and entry_buy_today and \
            bmaxCandleSizeOk and vwapnotfar

        ex9ConditionPattern = 18 <= baseActive <= 20
        exception9BuyEntry = ex9ConditionPattern and finBuyWindow and not buyWindowFired and \
            p.enable_buy_ex9 and entry_buy_today and closeAboveVWAP and vwapnotfar and \
            belowPP and belowPDH

        only25and28Failed_buy = baseActive == 21 and cond22 == 0
        exception10BuyEntry = only25and28Failed_buy and entry_buy_today and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex10

        closeAbove15mORBHigh = _b(ci > finalOrbHigh)
        baseActiveExcept11_25 = baseActive
        only11_25_28Failed = baseActiveExcept11_25 == 21 and cond22 == 0
        exception11BuyEntry = only11_25_28Failed and closeAbove15mORBHigh and isNewDayHigh and \
            finBuyWindow and not buyWindowFired and p.enable_buy_ex11 and vwapnotfar

        only11_17Failed_buy = baseActive == 21 and cond22 == 1
        exception12BuyEntry = only11_17Failed_buy and closeAbove15mORBHigh and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex12

        cond20Failed_ex13 = cond20 == 0
        cond22Failed_ex13 = cond22 == 0
        only17_26_28Failed_buy = baseActive == 20 and cond20Failed_ex13 and cond22Failed_ex13
        exception13BuyEntry = only17_26_28Failed_buy and entry_buy_today and finBuyWindow and \
            not buyWindowFired and p.enable_buy_ex13

        allActive_new = (normalEntry or exceptionBuyEntry or newExceptionBuyEntry or
                         exception4BuyEntry or exception5BuyEntry or exception6BuyEntry or
                         exception7BuyEntry or exception8BuyEntry or exception9BuyEntry or
                         exception10BuyEntry or exception11BuyEntry or exception12BuyEntry or
                         exception13BuyEntry)
        if allActive_new:
            buyWindowFired = True

        r_b_normal[i] = normalEntry
        r_b_ex[i] = exceptionBuyEntry
        r_b_ex17[i] = newExceptionBuyEntry
        r_b_ex4[i] = exception4BuyEntry
        r_b_ex5[i] = exception5BuyEntry
        r_b_ex6[i] = exception6BuyEntry
        r_b_ex7[i] = exception7BuyEntry
        r_b_ex8[i] = exception8BuyEntry
        r_b_ex9[i] = exception9BuyEntry
        r_b_ex10[i] = exception10BuyEntry
        r_b_ex11[i] = exception11BuyEntry
        r_b_ex12[i] = exception12BuyEntry
        r_b_ex13[i] = exception13BuyEntry
        r_b_any[i] = allActive_new
        r_base[i] = baseActive
        r_failed_b[i] = failedConditions + (1 if cond22 == 0 else 0)
        r_total[i] = I.total_score[i]

        # ==================================================================
        # SELL SIDE - MASTER ENTRIES (normal + Ex1 + Ex2-Ex19)
        # ==================================================================
        if i == 0:
            sbarsInSession_new = 0                     # sell-side own counter

        # -- first 5-min candle size including gap (SELL copy) --
        if isFirst5MinBar:
            sfirst5mHigh_new = hi
            sfirst5mLow_new = li
        sfirst5mRange = max(sfirst5mHigh_new, pdc) - min(sfirst5mLow_new, pdc) if not math.isnan(pdc) else NAN
        sfirst5mSizePct_new = sfirst5mRange / pdc * 100.0 if (not math.isnan(sfirst5mRange) and pdc != 0) else NAN
        first5size = _b(sfirst5mSizePct_new <= 2.5)
        ex5first5size = _b(sfirst5mSizePct_new <= 2.5)
        firstsellsize = _b(sfirst5mSizePct_new <= 3.5)

        if isFirst5MinBar:
            sfirst5mFullSize = ((hi - li) / ci) * 100.0
            sfirst5mBodySize = abs(ci - oi) / ci * 100.0

        # -- rejection from lows (bullish hammer block) --
        bbodySize = abs(ci - oi)
        btotalRange = hi - li
        bupperShadow = hi - max(oi, ci)
        blowerShadow = min(oi, ci) - li
        bvalidRange = btotalRange > 0.001
        isRejectionFromLows = bvalidRange and (blowerShadow > bupperShadow * 3) and \
            (blowerShadow > btotalRange * 0.25) and (ci > li + blowerShadow * 0.5)
        notisBullishHammer = not isRejectionFromLows

        # -- first 6 candles max size (SELL copy, ignore first) --
        if isFirst5MinBar:
            sbarsInSession_new = 0
            smaxFirst6Size_new = 0.0
        if isNew5MinBar and not isFirst5MinBar:
            sbarsInSession_new += 1
            if 2 <= sbarsInSession_new <= 6:
                scandleSize_new = (ci - oi) / ci * 100.0
                smaxFirst6Size_new = max(smaxFirst6Size_new, scandleSize_new)

        # -- max candle size from 2nd candle until entry --
        if isFirst5MinBar:
            sstopTrackingCandles = False
            smaxCandleSizeUntilEntry = 0.0
        if isNew5MinBar and not isFirst5MinBar and not sstopTrackingCandles:
            scandleSizeUntilEntry = abs((ci - oi) / ci * 100.0)
            smaxCandleSizeUntilEntry = max(smaxCandleSizeUntilEntry, scandleSizeUntilEntry)

        # -- candles closing above 15m VWAP --
        if isFirst5MinBar:
            scandlesAboveVwap = 0
        if isNew5MinBar and not isFirst5MinBar and not sstopTrackingCandles:
            if _b(ci > I.vwap_15[i]):
                scandlesAboveVwap += 1
        if isNewDay:
            scandlesAboveVwap = 0
        isVwapBearish = scandlesAboveVwap < 2

        # -- any full candle (OHLC) above 15m VWAP --
        if isFirst5MinBar:
            shasFullCandleAboveVwap = False
        if isNew5MinBar and not isFirst5MinBar and not sstopTrackingCandles:
            if _b(oi > I.vwap_15[i]) and _b(hi > I.vwap_15[i]) and \
               _b(li > I.vwap_15[i]) and _b(ci > I.vwap_15[i]):
                shasFullCandleAboveVwap = True
        if isNewDay:
            shasFullCandleAboveVwap = False

        # -- first 30min change (SELL copy, parity) --
        if isFirst5MinBar:
            spriceAt915_new = ci
            spriceAt945_new = NAN
        if sbarsInSession_new == 6 and math.isnan(spriceAt945_new):
            spriceAt945_new = ci

        # -- SELL retest machine 1 (support breakdown) --
        s1_supportLevel = I.s_support[i]
        if (not s1_breakdownOccurred) and _b(ci < s1_supportLevel) and not isFirst5MinBar:
            s1_breakdownOccurred = True
            s1_breakdownLevel = s1_supportLevel
            s1_barsSinceBreakdown = 0
        if s1_breakdownOccurred:
            s1_barsSinceBreakdown += 1
            tol = 0.003
            s1_isRetest = _b(hi >= s1_breakdownLevel * (1 - tol)) and \
                _b(hi <= s1_breakdownLevel * (1 + tol)) and _b(ci < s1_breakdownLevel)
            if s1_isRetest:
                s1_retestCompleted = True
            if s1_barsSinceBreakdown > 15:
                s1_breakdownOccurred = False

        # -- SELL BOS/CHoCH retest machine 2 --
        s2_isSwingLow = all(_b(li < x) for x in (l1, l2,
                                                 I.l[i - 3] if i >= 3 else NAN,
                                                 I.l[i - 4] if i >= 4 else NAN,
                                                 I.l[i - 5] if i >= 5 else NAN,
                                                 I.l[i - 6] if i >= 6 else NAN))
        if s2_isSwingLow and not s2_structureFormed:
            s2_recentLow = li
            s2_barsSinceLow = 0
            s2_structureLevel = h1
            s2_structureFormed = True
        if s2_structureFormed:
            s2_barsSinceLow += 1
            s2_higherLowFormed = _b(li > s2_recentLow) and s2_barsSinceLow >= 3
            if s2_higherLowFormed and _b(ci < s2_structureLevel) and not s2_breakdownOccurred:
                s2_breakdownOccurred = True
                s2_breakdownLevel = s2_structureLevel
                s2_barsSinceBreakdown = 0
                s2_retestCompleted = False
            if _b(ci < s2_recentLow):
                s2_structureFormed = False
        if s2_breakdownOccurred:
            s2_barsSinceBreakdown += 1
            tol = 0.003
            s2_isRetest = _b(hi >= s2_breakdownLevel * (1 - tol)) and \
                _b(hi <= s2_breakdownLevel * (1 + tol)) and _b(ci < s2_breakdownLevel)
            if s2_isRetest:
                s2_retestCompleted = True
            if s2_barsSinceBreakdown > 15:
                s2_breakdownOccurred = False

        # -- SELL condition stack (01-21 base + 22 break/retest) --
        scond01 = 1 if _b(I.rsi5[i] < (I.rsi5[i - 1] if i >= 1 else NAN)) else 0
        scond02 = 1 if _b(I.rsi5[i] < 45) else 0
        scond03 = 1 if _b(c0 < I.vwap5[i]) else 0
        scond04 = 1 if _b(c0 < c1) else 0
        scond05 = 1 if _b(I.atr5[i] > 0.5 * I.smaatr5[i]) else 0
        scond06 = 1 if _b(c0 < I.ema20_5[i]) else 0
        scond07 = 1 if _b(c0 < oi) else 0
        scond08 = 1 if _b(vi * c0 > 5000000) else 0
        scond09 = 1 if _b(c0 < l1) else 0
        scond10 = 1 if _b(I.vwap5[i] < (I.vwap5[i - 1] if i >= 1 else NAN)) else 0
        scond11 = 1 if _b(c0 < l2) else 0
        scond12 = 1 if _b((h0 - l0) / c0 < 0.015) else 0
        scond13 = 1 if _b(I._15[i] < I._15_open[i]) else 0
        scond14 = 1 if _b(ci < oi) else 0
        scond15 = 1 if _b(I.dOpen[i] > 50) else 0
        scond16 = 1 if _b(I.dClose[i] < I.ema39_D[i]) else 0
        scond17 = 1 if _b(I._15[i] < I.ema39_15[i]) else 0
        scond18 = 1 if _b(l0 < I.ll7_prev[i]) else 0
        scond19 = 1 if _b(sfirst5mSizePct_new < 5) else 0
        scond20 = 1 if _b(smaxFirst6Size_new < 1.5) else 0
        scond21 = 1 if _b(ci > I.dailyS2[i]) else 0

        s_retestDone = (s1_breakdownOccurred and s1_retestCompleted) or \
                       (s2_breakdownOccurred and s2_retestCompleted)
        s_before1015 = (wh < 10) or (wh == 10 and wm < 11)
        scond22 = 1 if (s_retestDone if s_before1015 else
                        (bool(I.s_original_breakdown[i]) or s_retestDone)) else 0
        r_scond22[i] = bool(scond22)

        sbaseActive = (scond01 + scond02 + scond03 + scond04 + scond05 + scond06 +
                       scond07 + scond08 + scond09 + scond10 + scond11 + scond12 +
                       scond13 + scond14 + scond15 + scond16 + scond17 + scond18 +
                       scond19 + scond20 + scond21)
        sfailedConditions = 21 - sbaseActive
        stotalFailedConditions = sfailedConditions + (1 if scond22 == 0 else 0)

        # -- SELL entry window + daily reset block --
        finSellWindow = _in_window(wh, wm, ssh, ssm, seh, sem)
        if isNewDay:
            sellWindowFired = False
            first5mHigh_check = 0.0
            s1_breakdownOccurred = False
            s1_retestCompleted = False
            s1_breakdownLevel = 0.0
            s1_barsSinceBreakdown = 0
            s2_structureLevel = 0.0
            s2_recentLow = 0.0
            s2_barsSinceLow = 0
            s2_structureFormed = False
            s2_breakdownOccurred = False
            s2_retestCompleted = False
            s2_breakdownLevel = 0.0
            s2_barsSinceBreakdown = 0
            sstopTrackingCandles = False
            smaxCandleSizeUntilEntry = 0.0
        if isFirst5MinBar:
            first5mHigh_check = hi
        first5mIsDayHigh = _b(first5mHigh_check == I.dHigh[i])

        s_brokePreDayLow = _b(ci < I.prev_day_low[i])
        if i > 0:
            todayLow_prev_end = todayLow                      # todayLow[1]
        else:
            todayLow_prev_end = NAN
        if isNewDay:
            todayLow = li
        else:
            todayLow = li if math.isnan(todayLow) else min(todayLow, li)
        if i == 0:
            todayLow = li
        isNewDayLow = _b(li <= todayLow_prev_end)
        isCloseBelowDayLow = _b(ci <= todayLow_prev_end)

        # -- candle range / relative spike tracking --
        if isFirst5MinBar:
            smaxCandleRangeUntilEntry = 0.0
            ssumCandleRangeUntilEntry = 0.0
            scandleCountUntilEntry = 0
        if isNew5MinBar and not isFirst5MinBar and not sstopTrackingCandles:
            scurrentRange = (hi - li) / ci * 100.0
            smaxCandleRangeUntilEntry = max(smaxCandleRangeUntilEntry, scurrentRange)
            ssumCandleRangeUntilEntry += scurrentRange
            scandleCountUntilEntry += 1
        savgRangeUntilEntry = (ssumCandleRangeUntilEntry / scandleCountUntilEntry
                               if scandleCountUntilEntry > 0 else 0.0)
        isNoRelativeSpike = smaxCandleRangeUntilEntry < savgRangeUntilEntry * 2.5
        if isNewDay:
            smaxCandleRangeUntilEntry = 0.0
            ssumCandleRangeUntilEntry = 0.0
            scandleCountUntilEntry = 0

        # -- trigger scores --
        sexceptionEntry = sfailedConditions < 2
        snormalTriggerScore = (sbaseActive + scond22) if s_before1015 else \
            (22 if sbaseActive >= 21 else
             (22 if (sbaseActive >= 20 and s_retestDone) else sbaseActive + scond22))
        suseExceptionEntry = sexceptionEntry
        closeBelow15mORBLow = _b(ci < finalOrbLow)

        # -- NORMAL + EX1 --
        snormalEntry = (snormalTriggerScore >= 22) and p.enable_sell_normal and \
            finSellWindow and not sellWindowFired and first5mIsDayHigh and \
            (not suseExceptionEntry or sbaseActive == 21) and \
            smaxCandleSizeUntilEntry < 2 and (isCloseBelowDayLow or entry_sell_today) and \
            closeBelow15mORBLow

        sexception1EntryReq = entry_sell_today if s_before1015 else \
            (entry_sell_today or s_brokePreDayLow)
        sexceptionSellEntry = suseExceptionEntry and (sbaseActive >= 21) and \
            p.enable_sell_ex1 and finSellWindow and not sellWindowFired and \
            sexception1EntryReq and smaxCandleSizeUntilEntry < 1.25 and first5size and \
            notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and entry_sell_today

        # -- EX2 / EX3 --
        sbaseActiveExcept8 = sbaseActive
        sellException2 = isNewDayLow and p.enable_sell_ex2 and (sbaseActiveExcept8 >= 21) and \
            finSellWindow and not sellWindowFired and not snormalEntry and \
            not sexceptionSellEntry and entry_sell_today and isCloseBelowDayLow and \
            notisBullishHammer

        sbaseActiveExcept17 = sbaseActive
        sellException3 = (sbaseActiveExcept17 >= 21) and isNewDayLow and p.enable_sell_ex3 and \
            finSellWindow and not sellWindowFired and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and isNoRelativeSpike and \
            notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isCloseBelowDayLow and (entry_sell_today or s_brokePreDayLow) and \
            smaxCandleSizeUntilEntry < 1.25

        # -- EX4 (only scond19+scond20 fail) --
        sbaseActiveExcept11_24_26 = (scond01 + scond02 + scond03 + scond04 + scond05 + scond06 +
                                     scond07 + scond08 + scond09 + scond10 + scond11 + scond12 +
                                     scond13 + scond14 + scond15 + scond16 + scond17 + scond18 +
                                     scond21)
        sellException4 = (scond19 == 0) and (scond20 == 0) and (sbaseActiveExcept11_24_26 >= 19) and \
            isNewDayLow and finSellWindow and not sellWindowFired and p.enable_sell_ex4 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and isCloseBelowDayLow and entry_sell_today and \
            notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and isNoRelativeSpike

        # -- EX5 (retest fail, 19-21 of 21 base) --
        sbaseActiveExcept11_28 = sbaseActive
        svalidStructure = (sbaseActiveExcept11_28 >= 19) and ((21 - sbaseActiveExcept11_28) <= 2)
        sellException5 = (scond22 == 0) and svalidStructure and \
            isNewDayLow and isCloseBelowDayLow and \
            (s_brokePreDayLow or entry_sell_today) and \
            smaxCandleSizeUntilEntry < 1.25 and notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and aboves3 and \
            p.enable_sell_ex5 and finSellWindow and not sellWindowFired

        # -- EX5s --
        sellException5s = (scond22 == 0) and (sbaseActiveExcept11_28 >= 21) and \
            isNewDayLow and p.enable_sell_ex5s and finSellWindow and not sellWindowFired and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException5 and \
            (entry_sell_today if s_before1015 else s_brokePreDayLow) and isCloseBelowDayLow and \
            ex5first5size and smaxCandleSizeUntilEntry < 2 and notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and isVwapBearish and aboves3

        # -- EX6 (retest fail + first5m not day high) --
        sbaseActiveExcept8_17_28_f5 = sbaseActive
        sellException6 = (scond22 == 0) and (not first5mIsDayHigh) and \
            (sbaseActiveExcept8_17_28_f5 >= 21) and isNewDayLow and p.enable_sell_ex6 and \
            finSellWindow and not sellWindowFired and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException5 and entry_sell_today and isCloseBelowDayLow and first5size

        # -- EX7 --
        sbaseActiveExcept25_28 = sbaseActive
        sellException7 = (scond22 == 0) and (sbaseActiveExcept25_28 >= 21) and \
            isNewDayLow and finSellWindow and p.enable_sell_ex7 and not sellWindowFired and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException5 and not sellException5s and \
            not sellException6 and (entry_sell_today or s_brokePreDayLow) and \
            first5size and smaxCandleSizeUntilEntry < 2 and notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and isCloseBelowDayLow and isVwapBearish

        # -- EX8 --
        sbaseActiveExcept8_11_14_28_f5 = sbaseActive
        sellException8 = (scond22 == 0) and (sbaseActiveExcept8_11_14_28_f5 >= 21) and \
            isNewDayLow and finSellWindow and p.enable_sell_ex8 and not sellWindowFired and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and smaxCandleSizeUntilEntry < 1.5 and \
            notisBullishHammer and isCloseBelowDayLow and isNoRelativeSpike

        # -- EX9 --
        sbaseActiveExcept11 = sbaseActive
        sellException9 = (sbaseActiveExcept11 >= 21) and isNewDayLow and finSellWindow and \
            not sellWindowFired and p.enable_sell_ex9 and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException4 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and not sellException8 and \
            isCloseBelowDayLow and notisBullishHammer and smaxCandleSizeUntilEntry < 1.5 and \
            entry_sell_today and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            first5size and isVwapBearish and isNoRelativeSpike

        # -- EX10 --
        sbaseActiveExcept11_14 = sbaseActive
        sellException10 = (sbaseActiveExcept11_14 >= 21) and isNewDayLow and finSellWindow and \
            not sellWindowFired and p.enable_sell_ex10 and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException4 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and not sellException8 and \
            not sellException9 and isCloseBelowDayLow and notisBullishHammer and \
            smaxCandleSizeUntilEntry < 1.25 and entry_sell_today and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and isNoRelativeSpike

        # -- EX11 (retest fail + first5m not day high) --
        sbaseActiveExcept8_28 = sbaseActive
        sellException11 = (scond22 == 0) and (not first5mIsDayHigh) and \
            (sbaseActiveExcept8_28 >= 21) and isNewDayLow and finSellWindow and \
            not sellWindowFired and p.enable_sell_ex11 and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException4 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and not sellException8 and \
            not sellException9 and not sellException10 and notisBullishHammer and \
            smaxCandleSizeUntilEntry < 1.5 and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and isCloseBelowDayLow

        # -- EX12 --
        sbaseActiveExcept15 = sbaseActive
        sellException12 = (sbaseActiveExcept15 >= 21) and isNewDayLow and finSellWindow and \
            not sellWindowFired and p.enable_sell_ex12 and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException4 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and not sellException8 and \
            not sellException9 and not sellException10 and not sellException11 and \
            isCloseBelowDayLow and smaxCandleSizeUntilEntry < 1.25 and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            (entry_sell_today or s_brokePreDayLow) and isVwapBearish and \
            isNoRelativeSpike and firstsellsize

        # -- EX13 --
        sbaseActiveExcept11_14_17 = sbaseActive
        sellException13 = (sbaseActiveExcept11_14_17 >= 21) and isNewDayLow and finSellWindow and \
            not sellWindowFired and p.enable_sell_ex13 and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and not sellException3 and \
            not sellException4 and not sellException5 and not sellException5s and \
            not sellException6 and not sellException7 and not sellException8 and \
            not sellException9 and not sellException10 and not sellException11 and \
            not sellException12 and isCloseBelowDayLow and notisBullishHammer and \
            entry_sell_today and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize)

        # -- EX14 --
        sbaseActiveExcept8_14_28 = sbaseActive
        sellException14 = (scond22 == 0) and (sbaseActiveExcept8_14_28 >= 21) and \
            isNewDayLow and finSellWindow and not sellWindowFired and p.enable_sell_ex14 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException4 and not sellException5 and \
            not sellException5s and not sellException6 and not sellException7 and \
            not sellException8 and not sellException9 and not sellException10 and \
            not sellException11 and not sellException12 and not sellException13 and \
            isCloseBelowDayLow and notisBullishHammer and smaxCandleSizeUntilEntry < 1.25 and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and entry_sell_today

        # -- EX15 (only snormal/Ex1/Ex2 excluded - Pine-faithful) --
        sbaseActiveExcept171 = sbaseActive
        sellException15 = (sbaseActiveExcept171 >= 21) and finSellWindow and \
            p.enable_sell_ex15 and not sellWindowFired and not snormalEntry and \
            not sexceptionSellEntry and not sellException2 and notisBullishHammer and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and entry_sell_today

        # -- EX16 --
        sbaseActiveExcept17_28 = sbaseActive
        sellException16 = (scond22 == 0) and (sbaseActiveExcept17_28 >= 21) and \
            isNewDayLow and finSellWindow and not sellWindowFired and p.enable_sell_ex16 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException4 and not sellException5 and \
            not sellException5s and not sellException6 and not sellException7 and \
            not sellException8 and not sellException9 and not sellException10 and \
            not sellException11 and not sellException12 and not sellException13 and \
            not sellException14 and not sellException15 and notisBullishHammer and \
            entry_sell_today

        # -- EX17 --
        sbaseActiveExcept14_17_28 = sbaseActive
        sellException17 = (scond22 == 0) and (sbaseActiveExcept14_17_28 >= 21) and \
            isNewDayLow and finSellWindow and not sellWindowFired and p.enable_sell_ex17 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException4 and not sellException5 and \
            not sellException5s and not sellException6 and not sellException7 and \
            not sellException8 and not sellException9 and not sellException10 and \
            not sellException11 and not sellException12 and not sellException13 and \
            not sellException14 and not sellException15 and not sellException16 and \
            isCloseBelowDayLow and notisBullishHammer and smaxCandleSizeUntilEntry < 1.25 and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and closeBelow15mORBLow

        # -- EX18 --
        sbaseActiveExcept28 = sbaseActive
        sellException18 = (scond22 == 0) and (sbaseActiveExcept28 >= 21) and \
            isNewDayLow and finSellWindow and not sellWindowFired and p.enable_sell_ex18 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException4 and not sellException5 and \
            not sellException5s and not sellException6 and not sellException7 and \
            not sellException8 and not sellException9 and not sellException10 and \
            not sellException11 and not sellException12 and not sellException13 and \
            not sellException14 and not sellException15 and not sellException16 and \
            not sellException17 and (isCloseBelowDayLow or entry_sell_today) and \
            notisBullishHammer and smaxCandleSizeUntilEntry < 1.25 and \
            (smaxCandleSizeUntilEntry <= sfirst5mFullSize or
             smaxCandleSizeUntilEntry <= sfirst5mBodySize) and \
            isNoRelativeSpike and closeBelow15mORBLow and isVwapBearish

        # -- EX19 (only scond18 + scond22 fail) --
        sbaseActiveExcept18_22 = (scond01 + scond02 + scond03 + scond04 + scond05 + scond06 +
                                  scond07 + scond08 + scond09 + scond10 + scond11 + scond12 +
                                  scond13 + scond14 + scond15 + scond16 + scond17 + scond19 +
                                  scond20 + scond21)
        sellException19 = (scond18 == 0) and (scond22 == 0) and (sbaseActiveExcept18_22 >= 20) and \
            finSellWindow and not sellWindowFired and p.enable_sell_ex19 and \
            not snormalEntry and not sexceptionSellEntry and not sellException2 and \
            not sellException3 and not sellException4 and not sellException5 and \
            not sellException5s and not sellException6 and not sellException7 and \
            not sellException8 and not sellException9 and not sellException10 and \
            not sellException11 and not sellException12 and not sellException13 and \
            not sellException14 and not sellException15 and not sellException16 and \
            not sellException17 and not sellException18 and notisBullishHammer and \
            closeBelow15mORBLow and isVwapBearish

        actualSellEntry = (snormalEntry or sexceptionSellEntry or sellException2 or
                           sellException3 or sellException4 or sellException5 or
                           sellException6 or sellException5s or sellException7 or
                           sellException8 or sellException9 or sellException10 or
                           sellException11 or sellException12 or sellException13 or
                           sellException14 or sellException15 or sellException16 or
                           sellException17 or sellException18 or sellException19)
        if actualSellEntry:
            sellWindowFired = True
            sstopTrackingCandles = True

        r_s_normal[i] = snormalEntry
        r_s_ex1[i] = sexceptionSellEntry
        r_s_ex2[i] = sellException2
        r_s_ex3[i] = sellException3
        r_s_ex4[i] = sellException4
        r_s_ex5[i] = sellException5
        r_s_ex5s[i] = sellException5s
        r_s_ex6[i] = sellException6
        r_s_ex7[i] = sellException7
        r_s_ex8[i] = sellException8
        r_s_ex9[i] = sellException9
        r_s_ex10[i] = sellException10
        r_s_ex11[i] = sellException11
        r_s_ex12[i] = sellException12
        r_s_ex13[i] = sellException13
        r_s_ex14[i] = sellException14
        r_s_ex15[i] = sellException15
        r_s_ex16[i] = sellException16
        r_s_ex17[i] = sellException17
        r_s_ex18[i] = sellException18
        r_s_ex19[i] = sellException19
        r_s_any[i] = actualSellEntry
        r_sbase[i] = sbaseActive
        r_failed_s[i] = stotalFailedConditions

        # ==================================================================
        # SCANNER EXCEPTION CODE  (f_scannerExceptionCode - same priority)
        # ==================================================================
        scannerBefore0945 = (wh < 9) or (wh == 9 and wm < 45)
        code = 0
        if exceptionBuyEntry:
            code = 101
        elif newExceptionBuyEntry:
            code = 102
        elif exception4BuyEntry:
            code = 103
        elif exception5BuyEntry:
            code = 104
        elif exception6BuyEntry:
            code = 105
        elif exception7BuyEntry:
            code = 106
        elif exception8BuyEntry:
            code = 107
        elif exception9BuyEntry:
            code = 108
        elif exception10BuyEntry:
            code = 109
        elif exception11BuyEntry:
            code = 110
        elif exception12BuyEntry:
            code = 111
        elif exception13BuyEntry:
            code = 112
        elif normalEntry:
            code = 80
        elif sexceptionSellEntry:
            code = 201
        elif sellException2:
            code = 202
        elif sellException3:
            code = 203
        elif sellException4:
            code = 204
        elif sellException5:
            code = 205
        elif sellException5s:
            code = 206
        elif sellException6:
            code = 207
        elif sellException7:
            code = 208
        elif sellException8:
            code = 209
        elif sellException9:
            code = 210
        elif sellException10:
            code = 211
        elif sellException11:
            code = 212
        elif sellException12:
            code = 213
        elif sellException13:
            code = 214
        elif sellException14:
            code = 215
        elif sellException15:
            code = 216
        elif sellException16:
            code = 217
        elif sellException17:
            code = 218
        elif sellException18:
            code = 219
        elif sellException19:
            code = 220
        elif snormalEntry:
            code = 280
        elif entry_buy_today and scannerBefore0945:
            code = 90
        elif entry_sell_today and scannerBefore0945:
            code = 290
        r_scan[i] = code

    out = pd.DataFrame(R, index=I.df.index)
    out["scan_name"] = out["scan_code"].map(lambda x: SCAN_CODE_NAME.get(int(x), ""))
    return out


# ==============================================================================
# 5. SECTOR / BATCH STOCK LISTS  (f_masterSym / f_masterName / f_masterEnabled)
# ==============================================================================
# (symbol, display name) per slot; the trailing count is f_masterEnabled's limit.
SECTORS: Dict[str, Dict[str, Tuple[List[Tuple[str, str]], int]]] = {
    "AUTO": {
        "Batch 1": ([("NSE:BAJAJ_AUTO", "Bajaj Auto"), ("NSE:BHARATFORG", "Bharat Forge"),
                     ("NSE:BOSCHLTD", "Bosch"), ("NSE:EICHERMOT", "Eicher Motors"),
                     ("NSE:EXIDEIND", "Exide Ind"), ("NSE:HEROMOTOCO", "Hero MotoCorp"),
                     ("NSE:M&M", "M&M"), ("NSE:MARUTI", "Maruti"),
                     ("NSE:MOTHERSON", "Motherson"), ("NSE:SONACOMS", "Sona Comstar"),
                     ("NSE:TVSMOTOR", "TVS Motor"), ("NSE:TMPV", "TMPV")], 12),
        "Batch 2": ([("NSE:TIINDIA", "Tube Investments"), ("NSE:UNOMINDA", "UNO Minda"),
                     ("NSE:ASHOKLEY", "Ashok Leyland")], 3),
    },
    "BANK ALL": {
        "Batch 1": ([("NSE:HDFCBANK", "HDFC Bank"), ("NSE:ICICIBANK", "ICICI Bank"),
                     ("NSE:SBIN", "SBI"), ("NSE:AXISBANK", "Axis Bank"),
                     ("NSE:KOTAKBANK", "Kotak Bank"), ("NSE:FEDERALBNK", "Federal Bank"),
                     ("NSE:CANBK", "Canara Bank"), ("NSE:BANKBARODA", "Bank of Baroda"),
                     ("NSE:IDFCFIRSTB", "IDFC First"), ("NSE:AUBANK", "AU Bank"),
                     ("NSE:PNB", "PNB"), ("NSE:BANDHANBNK", "Bandhan Bank")], 12),
        "Batch 2": ([("NSE:HDFCBANK", "NSE:RELIANCE")], 0),
    },
    "PRIVATE BANK": {
        "Batch 1": ([("NSE:AXISBANK", "Axis Bank"), ("NSE:BANDHANBNK", "Bandhan Bank"),
                     ("NSE:FEDERALBNK", "Federal Bank"), ("NSE:HDFCBANK", "HDFC Bank"),
                     ("NSE:ICICIBANK", "ICICI Bank"), ("NSE:IDFCFIRSTB", "IDFC First"),
                     ("NSE:INDUSINDBK", "IndusInd Bank"), ("NSE:KOTAKBANK", "Kotak Bank"),
                     ("NSE:RBLBANK", "RBL Bank"), ("NSE:YESBANK", "Yes Bank")], 10),
        "Batch 2": ([("NSE:AXISBANK", "NSE:RELIANCE")], 0),
    },
    "PSU BANK": {
        "Batch 1": ([("NSE:BANKBARODA", "Bank of Baroda"), ("NSE:BANKINDIA", "Bank of India"),
                     ("NSE:MAHABANK", "Bank of Maha"), ("NSE:CANBK", "Canara Bank"),
                     ("NSE:CENTRALBK", "Central Bank"), ("NSE:INDIANB", "Indian Bank"),
                     ("NSE:IOB", "IOB"), ("NSE:PSB", "Punjab & Sind"),
                     ("NSE:PNB", "PNB"), ("NSE:SBIN", "SBI"),
                     ("NSE:UCOBANK", "UCO Bank"), ("NSE:UNIONBANK", "Union Bank")], 12),
        "Batch 2": ([("NSE:BANKBARODA", "NSE:RELIANCE")], 0),
    },
    "FINANCE": {
        "Batch 1": ([("NSE:AXISBANK", "Axis Bank"), ("NSE:BSE", "BSE"),
                     ("NSE:BAJFINANCE", "Bajaj Finance"), ("NSE:BAJAJFINSV", "Bajaj Finserv"),
                     ("NSE:CHOLAFIN", "Chola Finance"), ("NSE:HDFCBANK", "HDFC Bank"),
                     ("NSE:HDFCLIFE", "HDFC Life"), ("NSE:ICICIBANK", "ICICI Bank"),
                     ("NSE:ICICIGI", "ICICI Lombard"), ("NSE:JIOFIN", "Jio Financial"),
                     ("NSE:KOTAKBANK", "Kotak Bank"), ("NSE:LICHSGFIN", "LIC Housing")], 12),
        "Batch 2": ([("NSE:MFSL", "Max Financial"), ("NSE:MUTHOOTFIN", "Muthoot Finance"),
                     ("NSE:PFC", "PFC"), ("NSE:RECLTD", "REC"),
                     ("NSE:SBICARD", "SBI Card"), ("NSE:SBILIFE", "SBI Life"),
                     ("NSE:SHRIRAMFIN", "Shriram Finance"), ("NSE:SBIN", "SBI")], 8),
    },
    "CONSUMER ELECTRONICS": {
        "Batch 1": ([("NSE:AMBER", "Amber Ent"), ("NSE:BATAINDIA", "Bata India"),
                     ("NSE:BLUESTARCO", "Blue Star"), ("NSE:CROMPTON", "Crompton"),
                     ("NSE:DIXON", "Dixon Tech"), ("NSE:HAVELLS", "Havells"),
                     ("NSE:KALYANKJIL", "Kalyan Jewellers"), ("NSE:LGEINDIA", "LG Electronics"),
                     ("NSE:PGEL", "PG Electroplast"), ("NSE:TITAN", "Titan"),
                     ("NSE:VOLTAS", "Voltas"), ("NSE:WHIRLPOOL", "Whirlpool")], 12),
        "Batch 2": ([("NSE:AMBER", "NSE:RELIANCE")], 0),
    },
    "FMCG": {
        "Batch 1": ([("NSE:HINDUNILVR", "HUL"), ("NSE:ITC", "ITC"),
                     ("NSE:NESTLEIND", "Nestle India"), ("NSE:VBL", "Varun Bev"),
                     ("NSE:TATACONSUM", "Tata Consumer"), ("NSE:MARICO", "Marico"),
                     ("NSE:BRITANNIA", "Britannia"), ("NSE:GODREJCP", "Godrej CP"),
                     ("NSE:DABUR", "Dabur"), ("NSE:COLPAL", "Colgate Palm"),
                     ("NSE:UBL", "UBL"), ("NSE:PATANJALI", "Patanjali Foods")], 12),
        "Batch 2": ([("NSE:HINDUNILVR", "NSE:RELIANCE")], 0),
    },
    "IT": {
        "Batch 1": ([("NSE:INFY", "Infosys"), ("NSE:TCS", "TCS"),
                     ("NSE:WIPRO", "Wipro"), ("NSE:COFORGE", "Coforge"),
                     ("NSE:HCLTECH", "HCL Tech"), ("NSE:PERSISTENT", "Persistent"),
                     ("NSE:TECHM", "Tech Mahindra"), ("NSE:MPHASIS", "Mphasis"),
                     ("NSE:TATAELXSI", "Tata Elxsi"), ("NSE:LTTS", "LTTS"),
                     ("NSE:LTIM", "LTIMindtree"), ("NSE:OFSS", "OFSS")], 12),
        "Batch 2": ([("NSE:INFY", "NSE:RELIANCE")], 0),
    },
    "MEDIA": {
        "Batch 1": ([("NSE:DBCORP", "D.B.Corp"), ("NSE:HATHWAY", "Hathway"),
                     ("NSE:NAZARA", "Nazara Tech"), ("NSE:NETWORK18", "Network18"),
                     ("NSE:PVRINOX", "PVR INOX"), ("NSE:PFOCUS", "Prime Focus"),
                     ("NSE:SAREGAMA", "Saregama"), ("NSE:SUNTV", "Sun TV"),
                     ("NSE:TIPSMUSIC", "Tips Music"), ("NSE:ZEEL", "Zee Ent")], 10),
        "Batch 2": ([("NSE:DBCORP", "NSE:RELIANCE")], 0),
    },
    "DEFENCE": {
        "Batch 1": ([("NSE:MTARTECH", "MTAR Tech"), ("NSE:APOLLO", "Apollo Micro"),
                     ("NSE:DATAPATTNS", "Data Patterns"), ("NSE:HAL", "HAL"),
                     ("NSE:BEL", "BEL"), ("NSE:PARAS", "Paras Defence"),
                     ("NSE:SOLARINDS", "Solar Inds"), ("NSE:GRSE", "GRSE"),
                     ("NSE:MAZDOCK", "Mazagon Dock"), ("NSE:BDL", "BDL"),
                     ("NSE:ASTRAMICRO", "Astra Micro"), ("NSE:COCHINSHIP", "Cochin Shipyard")], 12),
        "Batch 2": ([("NSE:MTARTECH", "NSE:RELIANCE")], 0),
    },
    "ENERGY": {
        "Batch 1": ([("NSE:COALINDIA", "Coal India"), ("NSE:NTPC", "NTPC"),
                     ("NSE:ADANIGREEN", "Adani Green"), ("NSE:POWERGRID", "Power Grid"),
                     ("NSE:TATAPOWER", "Tata Power"), ("NSE:POWERINDIA", "Power India"),
                     ("NSE:ENRIN", "ENRIN"), ("NSE:CGPOWER", "CG Power"),
                     ("NSE:GVT&D", "GVT&D"), ("NSE:THERMAX", "Thermax"),
                     ("NSE:ABB", "ABB"), ("NSE:SIEMENS", "Siemens")], 12),
        "Batch 2": ([("NSE:COALINDIA", "NSE:RELIANCE")], 0),
    },
    "OIL GAS": {
        "Batch 1": ([("NSE:RELIANCE", "Reliance"), ("NSE:ATGL", "Adani Total Gas"),
                     ("NSE:ONGC", "ONGC"), ("NSE:OIL", "Oil India"),
                     ("NSE:HINDPETRO", "HPCL"), ("NSE:BPCL", "BPCL"),
                     ("NSE:AEGISLOG", "Aegis Logistics"), ("NSE:IOC", "IOC"),
                     ("NSE:GAIL", "GAIL"), ("NSE:CHENNPETRO", "CPCL"),
                     ("NSE:PETRONET", "Petronet LNG"), ("NSE:IGL", "IGL")], 12),
        "Batch 2": ([("NSE:RELIANCE", "NSE:RELIANCE")], 0),
    },
    "METALS": {
        "Batch 1": ([("NSE:VEDL", "Vedanta"), ("NSE:HINDALCO", "Hindalco"),
                     ("NSE:NATIONALUM", "NALCO"), ("NSE:TATASTEEL", "Tata Steel"),
                     ("NSE:SAIL", "SAIL"), ("NSE:NMDC", "NMDC"),
                     ("NSE:HINDZINC", "Hind Zinc"), ("NSE:HINDCOPPER", "Hind Copper"),
                     ("NSE:JSWSTEEL", "JSW Steel"), ("NSE:JINDALSTEL", "Jindal Steel"),
                     ("NSE:APLAPOLLO", "APL Apollo"), ("NSE:WELCORP", "Welspun Corp")], 12),
        "Batch 2": ([("NSE:VEDL", "NSE:RELIANCE")], 0),
    },
    "PHARMA": {
        "Batch 1": ([("NSE:SUNPHARMA", "Sun Pharma"), ("NSE:LAURUSLABS", "Laurus Labs"),
                     ("NSE:TORNTPHARM", "Torrent Pharma"), ("NSE:CIPLA", "Cipla"),
                     ("NSE:DRREDDY", "Dr Reddy"), ("NSE:DIVISLAB", "Divis Lab"),
                     ("NSE:AUROPHARMA", "Auro Pharma"), ("NSE:LUPIN", "Lupin"),
                     ("NSE:ZYDUSLIFE", "Zydus Life"), ("NSE:GLENMARK", "Glenmark"),
                     ("NSE:MANKIND", "Mankind Pharma"), ("NSE:BIOCON", "Biocon")], 12),
        "Batch 2": ([("NSE:SUNPHARMA", "NSE:RELIANCE")], 0),
    },
    "PSU": {
        "Batch 1": ([("NSE:BHEL", "BHEL"), ("NSE:CONCOR", "CONCOR"),
                     ("NSE:HAL", "HAL"), ("NSE:IRCTC", "IRCTC"),
                     ("NSE:IRFC", "IRFC"), ("NSE:NHPC", "NHPC"),
                     ("NSE:NMDC", "NMDC"), ("NSE:NTPC", "NTPC"),
                     ("NSE:PFC", "PFC"), ("NSE:POWERGRID", "Power Grid"),
                     ("NSE:RECLTD", "REC"), ("NSE:RVNL", "RVNL")], 12),
        "Batch 2": ([("NSE:BHEL", "NSE:RELIANCE")], 0),
    },
    "REALTY": {
        "Batch 1": ([("NSE:ABREL", "AB Real Estate"), ("NSE:ANANTRAJ", "Anant Raj"),
                     ("NSE:BRIGADE", "Brigade Ent"), ("NSE:DLF", "DLF"),
                     ("NSE:GODREJPROP", "Godrej Prop"), ("NSE:LODHA", "Lodha"),
                     ("NSE:OBEROIRLTY", "Oberoi Realty"), ("NSE:PHOENIXLTD", "Phoenix Mills"),
                     ("NSE:PRESTIGE", "Prestige"), ("NSE:SOBHA", "Sobha")], 10),
        "Batch 2": ([("NSE:ABREL", "NSE:RELIANCE")], 0),
    },
}


def scanner_signal_rank(code: int) -> int:
    """f_scannerSignalRank - 3 exception, 2 normal, 1 base entry, 0 none."""
    if 101 <= code <= 220:
        return 3
    if code in (80, 280):
        return 2
    if code in (90, 290):
        return 1
    return 0


def scan_market(chart_times: pd.DatetimeIndex,
                symbol_frames: Dict[str, pd.DataFrame],
                params: Optional[Params] = None) -> pd.DataFrame:
    """Replicates the on-chart scanner table storage logic.

    chart_times  : 5-minute bar times of the chart symbol (the timeline on
                   which TradingView evaluates the scanner requests).
    symbol_frames: {display_name: result DataFrame of run_symbol()} for the
                   enabled sector/batch stocks (max 12).
    Returns the final per-day table rows (stock, signal, time) sorted by time,
    plus a log of every storage update (audit trail).
    """
    p = params or Params()
    # align each symbol's scan_code to the chart timeline (gaps_off => ffill)
    aligned = {}
    for name, res in symbol_frames.items():
        s = res["scan_code"].reindex(chart_times.union(res.index)).ffill().reindex(chart_times)
        aligned[name] = s.fillna(0).astype(int).to_numpy()

    n = len(chart_times)
    win_h = chart_times.hour.to_numpy()
    win_m = chart_times.minute.to_numpy()
    dates = chart_times.date
    day_change = np.zeros(n, dtype=bool)
    if n > 1:
        day_change[1:] = dates[1:] != dates[:-1]
    if n:
        day_change[0] = True                     # storage starts fresh with the data

    today_codes = {name: 0 for name in aligned}
    today_times = {name: "" for name in aligned}
    hits: List[Tuple[pd.Timestamp, str, int, str]] = []

    for i in range(n):
        before0945 = (win_h[i] < 9) or (win_h[i] == 9 and win_m[i] < 45)
        if day_change[i]:
            for name in aligned:
                today_codes[name] = 0
                today_times[name] = ""
        for name, arr in aligned.items():
            code = int(arr[i])
            if code != 0 and (today_codes[name] == 0 or
                              scanner_signal_rank(code) > scanner_signal_rank(today_codes[name])):
                today_codes[name] = code
                today_times[name] = f"{win_h[i]:02d}:{win_m[i]:02d}"
                hits.append((chart_times[i], name, code,
                             SCAN_CODE_NAME.get(code, "")))
        if not before0945:
            for name in aligned:
                if today_codes[name] in (90, 290):
                    today_codes[name] = 0
                    today_times[name] = ""

    rows = [(name, today_codes[name], today_times[name]) for name in aligned
            if today_codes[name] != 0]
    rows.sort(key=lambda r: (int(r[2][:2]) * 60 + int(r[2][3:])) if r[2] else 0)
    table = pd.DataFrame([{"stock": name, "signal": SCAN_CODE_NAME.get(code, ""),
                           "code": code, "time": tme} for name, code, tme in rows])
    log = pd.DataFrame(hits, columns=["chart_time", "stock", "code", "signal"])
    return table, log


def load_ohlcv_csv(path: str, tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Load a 5-min OHLCV CSV (datetime,open,high,low,close,volume) and set a
    tz-aware DatetimeIndex in the exchange timezone."""
    df = pd.read_csv(path)
    time_col = df.columns[0]
    df[time_col] = pd.to_datetime(df[time_col])
    df = df.set_index(time_col).sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize(tz)
    else:
        df.index = df.index.tz_convert(tz)
    df.columns = [str(x).lower() for x in df.columns]
    return df[["open", "high", "low", "close", "volume"]]


if __name__ == "__main__":  # quick demo: python master_scanner.py data.csv
    import sys
    if len(sys.argv) > 1:
        df_in = load_ohlcv_csv(sys.argv[1])
        res = run_symbol(df_in)
        sig_cols = [c for c in res.columns if c.endswith("today") or
                    c.startswith("buy_") or c.startswith("sell_") or
                    c in ("allActive_new", "actualSellEntry", "bullish5mBreakout",
                          "bearish5mBreakout")]
        signal_rows = res[res[sig_cols].any(axis=1)]
        cols = ["entry_buy_today", "entry_sell_today", "buy_normalEntry",
                "buy_exceptionEntry", "buy_newExceptionEntry",
                *[f"buy_exception{k}" for k in range(4, 14)],
                "sell_normalEntry", "sell_exception1",
                *[f"sell_exception{k}" for k in list(range(2, 20))],
                "scan_code", "scan_name"]
        existing = [c for c in cols if c in signal_rows.columns]
        print(signal_rows[existing].to_string())
        res.to_csv(sys.argv[1].replace(".csv", "_signals.csv"))
        print("\nFull result written to:", sys.argv[1].replace(".csv", "_signals.csv"))
    else:
        print("usage: python master_scanner.py <5min_ohlcv.csv>")
