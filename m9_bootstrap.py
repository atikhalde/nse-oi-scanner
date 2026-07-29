#!/usr/bin/env python3
"""M9 daily history bootstrap — downloads ~60d of 5-min bars for the full
1,050-symbol M9 cash universe (m9_universe.csv) into data/history9/.

Ladder identical to live_runner.mode_bootstrap: Dhan (preferred) -> Yahoo.
Runs from the 2. Bootstrap workflow right after the F&O refresh and BEFORE
label_learn, so the learning-log labeler always has fresh M9 bars.
Non-fatal by design (the workflow armors it with `|| echo`).

Usage: python -u m9_bootstrap.py
"""
import os
import sys
import time

import pandas as pd

import live_runner as L
import feeds

HIST9 = L.ROOT / "data" / "history9"


def main():
    now = L.now_ist()
    uni = pd.read_csv(L.ROOT / "m9_universe.csv")
    sid = dict(zip(uni["symbol"], uni["dhan_security_id"]))
    since = (now - L.dt.timedelta(days=63)).strftime("%Y-%m-%d 09:15:00")
    to = now.strftime("%Y-%m-%d %H:%M:%S")
    HIST9.mkdir(parents=True, exist_ok=True)
    n_ok = n_dhan = n_yahoo = n_fail = 0
    t0 = time.time()
    for sym in uni["symbol"]:
        df = None
        if os.environ.get("DHAN_TOKEN"):
            try:
                df = feeds.fetch_bars_dhan(sid[sym], since, to)
                time.sleep(0.6)
            except Exception as e:
                print(f"  dhan {sym}: {e}")
        if df is not None and not df.empty:
            n_dhan += 1
        else:
            df = feeds.fetch_bars_yahoo(sym, "60d")
            time.sleep(1.2)
            if df is not None and not df.empty:
                n_yahoo += 1
        if df is None or df.empty:
            print(f"  !! {sym}: no data")
            n_fail += 1
            continue
        df[["dt", "open", "high", "low", "close", "volume"]].to_csv(HIST9 / f"{sym}.csv", index=False)
        n_ok += 1
        if n_ok % 50 == 0:
            el = time.time() - t0
            print(f"  {n_ok}/{len(uni)} · {el / 60:.1f} min")
    print(f"M9 bootstrap done: {n_ok}/{len(uni)} symbols "
          f"(dhan {n_dhan} · yahoo {n_yahoo} · failed {n_fail}) · {(time.time() - t0) / 60:.1f} min")
    return 0 if n_ok else 1


if __name__ == "__main__":
    sys.exit(main())
