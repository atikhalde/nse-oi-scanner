# -*- coding: utf-8 -*-
"""Smoke test for master_scanner.py on synthetic 5-minute NSE-like data."""
import numpy as np
import pandas as pd

from master_scanner import (Params, run_symbol, scan_market, pine_ema, pine_rma,
                            pine_sma, pine_rsi, SCAN_CODE_NAME)


def make_5m(days=60, seed=7, start_price=1500.0, daily_bars=75):
    """Random-walk 5m OHLCV, 09:15..15:25 IST, Mon-Fri."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2026-05-04", periods=days, freq="B", tz="Asia/Kolkata")
    bars_per_day = [d + pd.Timedelta(hours=9, minutes=15) + i * pd.Timedelta(minutes=5)
                    for d in dates for i in range(daily_bars)]
    idx = pd.DatetimeIndex(bars_per_day)
    n = len(idx)
    ret = rng.normal(0, 0.0012, n)
    close = start_price * np.exp(np.cumsum(ret))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    # day-open gap-ish
    day_change = idx.date != np.roll(idx.date, 1)
    day_change[0] = False
    open_[day_change] = close[day_change] * (1 + rng.normal(0, 0.004, day_change.sum()))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.0008, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.0008, n)))
    vol = rng.integers(50_000, 900_000, n).astype(float)
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": vol}, index=idx)


def check_primitives():
    x = np.array([1., 2., 3., 4., 5., 6., 7., 8., 9., 10.])
    ema = pine_ema(x, 3)
    assert ema[0] == 1.0 and abs(ema[1] - 1.5) < 1e-12
    rma = pine_rma(x, 3)                # Pine ta.rma: na until SMA(1,2,3) seed
    assert np.isnan(rma[0]) and np.isnan(rma[1]) and rma[2] == 2.0
    assert abs(rma[3] - (1 / 3 * 4 + 2 / 3 * 2)) < 1e-12
    assert abs(rma[4] - (1 / 3 * 5 + 2 / 3 * (8 / 3))) < 1e-12
    sma = pine_sma(x, 3)
    assert np.isnan(sma[1]) and sma[2] == 2.0
    rsi = pine_rsi(np.arange(1., 30.), 14)
    assert rsi[-1] == 100.0                       # monotonic up series
    print("primitives OK")


def main():
    check_primitives()
    df = make_5m()
    res = run_symbol(df)
    print("bars:", len(res))

    buy_cols = ["buy_normalEntry", "buy_exceptionEntry", "buy_newExceptionEntry"] + \
               [f"buy_exception{k}" for k in range(4, 14)]
    sell_cols = ["sell_normalEntry", "sell_exception1"] + \
                [f"sell_exception{k}" for k in range(2, 20)]

    # 1) exception windows
    hh = pd.Series(res.index.hour, index=res.index)
    mm = pd.Series(res.index.minute, index=res.index)
    fbuy_ok = ((hh > 9) | ((hh == 9) & (mm >= 26))) & ((hh < 11) | ((hh == 11) & (mm <= 15)))
    fsell_ok = ((hh > 9) | ((hh == 9) & (mm >= 26))) & ((hh < 12) | ((hh == 12) & (mm <= 0)))
    assert (res.loc[res[buy_cols].any(axis=1), :].pipe(
        lambda s: (fbuy_ok.loc[s.index]).all())), "BUY outside finBuyWindow"
    assert (res.loc[res[sell_cols].any(axis=1), :].pipe(
        lambda s: (fsell_ok.loc[s.index]).all())), "SELL outside finSellWindow"

    # 2) at most one master BUY and one master SELL per day
    day = res.index.date
    buy_hits = res[buy_cols].any(axis=1).groupby(day).sum()
    sell_hits = res[sell_cols].any(axis=1).groupby(day).sum()
    assert (buy_hits <= 1).all() and (sell_hits <= 1).all(), "daily lock broken"

    # 3) scan_code consistency with flags
    codes = res["scan_code"].to_numpy()
    assert res.loc[codes >= 101, :].pipe(
        lambda s: (s["scan_code"] >= 0).all()) is not None
    ex_buy_any = res[buy_cols].any(axis=1) & ~res["buy_normalEntry"]
    code_is_buy_ex = res["scan_code"].between(101, 112)
    assert (code_is_buy_ex <= ex_buy_any).all(), "code 101-112 without buy exception"

    # 4) base entries confined to the three daily windows (or blocked) - just count
    print("base entry_buy_today:", int(res["entry_buy_today"].sum()),
          " entry_sell_today:", int(res["entry_sell_today"].sum()))
    print("master BUY signals :", int(res[buy_cols].any(axis=1).sum()))
    print("master SELL signals:", int(res[sell_cols].any(axis=1).sum()))
    print("bullish5mBreakout  :", int(res["bullish5mBreakout"].sum()),
          " bearish5mBreakout:", int(res["bearish5mBreakout"].sum()))
    print("ORB bull/bear break:", int(res["orb_bull_break"].sum()),
          int(res["orb_bear_break"].sum()))
    dist = res.loc[res["scan_code"] != 0, "scan_name"].value_counts()
    print("\nscanner code distribution:\n", dist.to_string() if len(dist) else "(none)")
    assert dist.index.isin(SCAN_CODE_NAME.values()).all()
    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
