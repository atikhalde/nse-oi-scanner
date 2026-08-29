"""Chronological replay evaluator for M14 Ultra High-Conviction A+ Entry Model."""
import glob
import json
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import costs
import m14_entry as E
import m14_trader as T
import master_scanner as ms
import trader

LEARN = ROOT / "learn"


def evaluate_m14_replay():
    labeled_files = sorted(glob.glob(str(LEARN / "labeled_*.csv")))
    if not labeled_files:
        print("No labeled CSV files found for replay.")
        return

    dfs = [pd.read_csv(f) for f in labeled_files]
    df = pd.concat(dfs, ignore_index=True)

    for col in ["cf_net", "cf_r", "mfe_r", "mae_r", "breadth", "daypct", "rank"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["win"] = df["cf_net"] > 0
    df["hour"] = df["time"].str[:2].astype(int)
    df["time_min"] = df["hour"] * 60 + df["time"].str[3:5].astype(int)

    # Apply M14 Ultra High-Conviction Filters
    real = df[
        (~df["code"].isin([90, 290])) &
        (df["time_min"] >= 585) & (df["time_min"] <= 690) &
        (df["code"].isin([101, 102, 104, 106, 107, 201, 202, 208, 209, 212, 213, 280])) &
        (df["rank"] <= 10) &
        (df["cls"].isin(["BREAKOUT", "S1", "S1+S3", "S1+S4", "S1+S2", "PULLBACK"]))
    ].copy()

    buy_mask = (real["side"] == "BUY") & (real["bear_close"] <= 0.48) & (~real["day_regime"].isin(["TREND-DOWN", "V-REVERSAL / bear-trap"]))
    sell_mask = (real["side"] == "SELL") & (real["bear_close"] >= 0.52) & (~real["day_regime"].isin(["TREND-UP", "V-REVERSAL / bear-trap"]))

    m14_filtered = real[buy_mask | sell_mask].copy()
    m14_filtered = m14_filtered.drop_duplicates(subset=["day", "sym"])

    daily_capped = []
    for d, g in m14_filtered.groupby("day"):
        daily_capped.append(g.sort_values(by=["rank", "time"]).head(3))

    res_df = pd.concat(daily_capped, ignore_index=True) if daily_capped else pd.DataFrame()

    total_sessions = int(df["day"].nunique())
    traded_sessions = int(res_df["day"].nunique()) if not res_df.empty else 0
    total_trades = int(len(res_df))
    wins = int((res_df["cf_net"] > 0).sum()) if not res_df.empty else 0
    losses = int((res_df["cf_net"] <= 0).sum()) if not res_df.empty else 0
    win_rate = float(wins / total_trades * 100.0) if total_trades > 0 else 0.0

    net_pnl = float(res_df["cf_net"].sum()) if not res_df.empty else 0.0
    expectancy = float(net_pnl / total_trades) if total_trades > 0 else 0.0

    win_sum = float(res_df[res_df["cf_net"] > 0]["cf_net"].sum()) if not res_df.empty else 0.0
    loss_sum = float(abs(res_df[res_df["cf_net"] < 0]["cf_net"].sum())) if not res_df.empty else 0.0
    pf = float(win_sum / loss_sum) if loss_sum > 0 else 999.0

    output = {
        "model": "M14 Ultra High-Conviction A+",
        "total_sessions": total_sessions,
        "traded_sessions": traded_sessions,
        "total_trades": total_trades,
        "trades_per_day": round(float(total_trades / traded_sessions), 2) if traded_sessions > 0 else 0.0,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "net_pnl_rs": round(net_pnl, 0),
        "expectancy_rs": round(expectancy, 1),
        "profit_factor": round(pf, 2),
    }

    out_file = ROOT / "analysis" / "output" / "m14_evaluation.json"
    out_file.parent.mkdir(exist_ok=True)
    out_file.write_text(json.dumps(output, indent=2))

    print("=== M14 REPLAY EVALUATION REPORT ===")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    evaluate_m14_replay()
