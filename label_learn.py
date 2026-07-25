#!/usr/bin/env python3
"""Phase-0 LABELER — adds outcome labels to learn/raw_*.csv (user pick A, 25-Jul-2026).

Runs in the 08:45 IST bootstrap job AFTER history refresh. For every day whose raw files
are newer than learn/labeled_{day}.csv (or it doesn't exist), it re-evaluates each logged
candidate over that day's 5m bars from data/history/*.csv with the LIVE v3 trade config
(trader.evaluate, structure SL ∓0.02%, no targets, +1R trail, ₹50k/₹900, 15:20 sq-off)
plus July-2026 costs — so labels match live economics.

Labels added per row:
  cf_net, cf_r, cf_exit   counterfactual v3 outcome (₹ net, R, exit text)
  mfe_r, mae_r            max favorable/adverse excursion after entry (in R vs structure SL)
  mfe30_r, mfe60_r        MFE within 30/60 min of entry (in R)
  eod_pct                 signed % move entry→close on the signal side
  bear_close              % of history universe below day open at close (breadth)
  day_regime              V-REVERSAL / TREND-DOWN / TREND-UP / MIXED (M5 rules)
Usage: python -u label_learn.py [--day YYYY-MM-DD] [--force]
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
HIST = ROOT / "data" / "history"
LEARN = ROOT / "learn"

import trader, costs  # noqa: E402

THRESH_V = dict(gaps=70, bear1030=0.55, green=0.55, bear_tr_dn=(0.70, 0.65), bear_tr_up=(0.30, 0.35))

_DF = {}


def _load(sym):
    if sym not in _DF:
        fp = HIST / f"{sym}.csv"
        if not fp.exists():
            _DF[sym] = None
        else:
            h = pd.read_csv(fp, parse_dates=["dt"])
            h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata") \
                if h["dt"].dt.tz is None else h["dt"]
            h["day"] = h["dt"].dt.strftime("%Y-%m-%d")
            h["t"] = h["dt"].dt.strftime("%H:%M")
            _DF[sym] = h
    return _DF[sym]


def day_universe_stats(day):
    """breadth checkpoints over the history universe + regime label (M5 thresholds)."""
    bear_close = bear_1030 = green = gaps = n = 0
    for fp in HIST.glob("*.csv"):
        h = _load(fp.stem)
        if h is None:
            continue
        t = h[h.day == day]
        yd = h[h.day < day]
        if t.empty or yd.empty:
            continue
        n += 1
        o, c = float(t.open.iloc[0]), float(t.close.iloc[-1])
        pc = float(yd.close.iloc[-1])
        bear_close += c < o
        green += c > pc
        gaps += abs(o / pc - 1) > 0.004
        t1030 = t[t.t <= "10:30"]
        if len(t1030):
            bear_1030 += float(t1030.close.iloc[-1]) < o
    if not n:
        return dict(bear_close="", day_regime="")
    bc, b10, gr = bear_close / n, bear_1030 / n, green / n
    if gaps >= THRESH_V["gaps"] and b10 >= THRESH_V["bear1030"] and gr >= THRESH_V["green"]:
        reg = "V-REVERSAL / bear-trap"
    elif b10 >= THRESH_V["bear_tr_dn"][0] and bc >= THRESH_V["bear_tr_dn"][1]:
        reg = "TREND-DOWN"
    elif b10 <= THRESH_V["bear_tr_up"][0] and bc <= THRESH_V["bear_tr_up"][1]:
        reg = "TREND-UP"
    else:
        reg = "MIXED"
    return dict(bear_close=round(bc, 3), day_regime=reg)


def label_row(r):
    """counterfactual v3 outcome + excursion labels for one candidate row."""
    sym = str(r["sym"])
    h = _load(sym)
    out = dict(cf_net="", cf_r="", cf_exit="", mfe_r="", mae_r="", mfe30_r="", mfe60_r="", eod_pct="")
    if h is None or not str(r.get("entry", "")).strip():
        return out
    t = h[h.day == str(r["day"])].reset_index(drop=True)
    if t.empty:
        return out
    etime = str(r["time"])
    side = str(r["side"])
    s = 1 if side == "BUY" else -1
    try:
        tr = trader.evaluate(sym, side, etime, float(r["entry"]), str(r["sig"]), t,
                             warmup=trader.load_warmup(HIST / f"{sym}.csv", str(r["day"])),
                             today_date=str(r["day"]), sl_mode="structure")
        if "error" not in tr:
            c = costs.trade_costs(tr)
            out.update(cf_net=round(c["net"]), cf_r=tr["r_total"], cf_exit=tr["exit_text"][:60])
        post = t[t.t >= etime]
        if len(post):
            risk = float(tr.get("risk_pts", 0) or 0)
            if "error" not in tr and risk > 0:
                if s == 1:
                    fav = post.high.astype(float) - float(r["entry"])
                    adv = float(r["entry"]) - post.low.astype(float)
                else:
                    fav = float(r["entry"]) - post.low.astype(float)
                    adv = post.high.astype(float) - float(r["entry"])
                out["mfe_r"] = round(fav.max() / risk, 2)
                out["mae_r"] = round(adv.max() / risk, 2)
                hh, mm0 = int(etime[:2]), int(etime[3:5])
                for win in (30, 60):
                    endm = hh * 60 + mm0 + win
                    end = f"{endm // 60:02d}:{endm % 60:02d}"
                    pw = post[post.t <= end]
                    if len(pw):
                        fw = ((pw.high.astype(float) - float(r["entry"])) if s == 1
                              else (float(r["entry"]) - pw.low.astype(float)))
                        out[f"mfe{win}_r"] = round(fw.max() / risk, 2)
            out["eod_pct"] = round(s * (float(t.close.iloc[-1]) / float(r["entry"]) - 1) * 100, 2)
    except Exception as e:
        out["cf_exit"] = f"{out.get('cf_exit', '')} [label-err {type(e).__name__}]"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    raws = sorted(LEARN.glob("raw_*_*.csv"))
    days = {}
    for fp in raws:
        d = fp.stem.split("_", 2)[2]
        days.setdefault(d, []).append(fp)
    for day, files in sorted(days.items()):
        if a.day and day != a.day:
            continue
        out_fp = LEARN / f"labeled_{day}.csv"
        if not a.force and out_fp.exists() and all(out_fp.stat().st_mtime > f.stat().st_mtime for f in files):
            continue
        frames = []
        for f in files:
            try:
                frames.append(pd.read_csv(f, dtype=str).fillna(""))
            except Exception as e:
                print(f"  label: skip {f.name}: {e}")
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        stats = day_universe_stats(day)
        lbl = df.apply(label_row, axis=1, result_type="expand")
        df = pd.concat([df, lbl], axis=1)
        df["bear_close"] = stats["bear_close"]
        df["day_regime"] = stats["day_regime"]
        df.to_csv(out_fp, index=False)
        ok = (df["cf_net"] != "").sum()
        print(f"labeled {day}: {len(df)} rows ({ok} evaluated) · regime {stats['day_regime']} → {out_fp.name}")


if __name__ == "__main__":
    main()
