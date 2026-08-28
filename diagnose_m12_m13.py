#!/usr/bin/env python3
"""M12 / M13 "why is nothing firing" audit — reproducible breakdown.

Reads only committed artefacts (learn/{m12,m13}_candidates_*.csv, state12.json,
state13.json, data/history/*.csv, data/m12_prev_close.json, data/m13_prev_context.json)
and prints the gate-by-gate funnel, the timing funnel, and the data-freshness verdict.

No network, no trading state mutation, no model logic change.

    python -u diagnose_m12_m13.py            # everything
    python -u diagnose_m12_m13.py --day 2026-08-28
    python -u diagnose_m12_m13.py --json out.json

Exit code is 1 when a blocking defect is detected, so CI can assert on it.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Frozen rule sets, mirrored here on purpose so this file is a *check* on the
# models rather than a second source of truth imported from them.
M12_WHITELIST = {102, 201, 202, 209, 213, 216, 220, 280}
M12_WINDOW = ("09:45", "12:00")
M12_DIR_PREV = (-1.00, 0.20)
M12_SECTOR_BREADTH_MAX = 0.43
M13_REAL_CODES = {80, 280} | set(range(101, 113)) | set(range(201, 221))
M13_PREVIEW_CODES = {90, 290}
AGE_LIMIT_MIN = 5.0
MIN_PREV_COVERAGE = 180
SQOFF = "15:20"


def _num(df, col):
    if col not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index)
    return pd.to_numeric(df[col], errors="coerce")


def load_ledgers(model: str, day: str | None) -> pd.DataFrame:
    frames = []
    for fp in sorted(glob.glob(str(ROOT / "learn" / f"{model}_candidates_*.csv"))):
        if os.path.getsize(fp) < 16:
            continue
        stamp = os.path.basename(fp).split("_")[-1][:10]
        if day and stamp != day:
            continue
        try:
            df = pd.read_csv(fp)
        except Exception:
            continue
        if not df.empty:
            df.insert(0, "day", stamp)
            frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def funnel_m12(d: pd.DataFrame) -> list[tuple[str, int, int]]:
    n = len(d)
    dp = _num(d, "f_dir_prev_pct")
    sb = _num(d, "f_sector_breadth_prev_dir")
    vc = _num(d, "f_video_setup_count")
    cv = _num(d, "f_clock_relvol")
    in_win = (d["time"] >= M12_WINDOW[0]) & (d["time"] <= M12_WINDOW[1])
    return [
        ("1. code in frozen reliability whitelist", int(d["code"].isin(M12_WHITELIST).sum()), n),
        ("2. signal time inside 09:45-12:00 window", int(in_win.sum()), n),
        ("3. anti-chase displacement in [-1.00%, +0.20%]", int(((dp >= M12_DIR_PREV[0]) & (dp <= M12_DIR_PREV[1])).sum()), n),
        ("4. sector breadth vs prev close <= 0.43", int((sb <= M12_SECTOR_BREADTH_MAX).sum()), n),
        ("5. at least one causal video setup", int((vc >= 1).sum()), n),
        ("6. same-clock relative volume >= 1.0", int((cv >= 1.0).sum()), n),
        ("7. model verdict accepted (all six + causality)", int(_num(d, "accepted").fillna(0).astype(bool).sum()), n),
    ]


def funnel_m13(d: pd.DataFrame) -> list[tuple[str, int, int]]:
    n = len(d)
    real = d["code"].isin(M13_REAL_CODES) & ~d["code"].isin(M13_PREVIEW_CODES)
    in_win = (d["time"] >= M12_WINDOW[0]) & (d["time"] <= M12_WINDOW[1])
    s1 = _num(d, "f_s1")
    ob = _num(d, "f_opening_breadth_dir")
    lb = _num(d, "f_live_breadth_dir")
    vix = _num(d, "f_prior_vix_return")
    sc = _num(d, "score")
    return [
        ("1. real enabled chart master variant", int(real.sum()), n),
        ("2. signal time inside 09:45-12:00 window", int(in_win.sum()), n),
        ("3. S1 Morning Base mandatory", int((s1 == 1).sum()), n),
        ("4. opening market breadth >= 55% in signal direction", int((ob >= 0.55).sum()), n),
        ("5. prior-session VIX return > -2%", int((vix > -2.0).sum()), n),
        ("6. live breadth not flipped opposite (<45% veto)", int((lb >= 0.45).sum()), n),
        ("7. A+ score >= 70", int((sc >= 70.0).sum()), n),
        ("8. model verdict accepted", int(_num(d, "accepted").fillna(0).astype(bool).sum()), n),
    ]


def and_all(d: pd.DataFrame, model: str) -> pd.Series:
    """Rows that satisfy every hard gate, evaluated independently of short-circuit order."""
    if model == "m12":
        dp = _num(d, "f_dir_prev_pct")
        sb = _num(d, "f_sector_breadth_prev_dir")
        vc = _num(d, "f_video_setup_count")
        cv = _num(d, "f_clock_relvol")
        return (d["code"].isin(M12_WHITELIST)
                & (d["time"].between(*M12_WINDOW))
                & (dp >= M12_DIR_PREV[0]) & (dp <= M12_DIR_PREV[1])
                & (sb <= M12_SECTOR_BREADTH_MAX)
                & (vc >= 1) & (cv >= 1.0)).fillna(False)
    ob = _num(d, "f_opening_breadth_dir")
    lb = _num(d, "f_live_breadth_dir")
    return (d["code"].isin(M13_REAL_CODES) & ~d["code"].isin(M13_PREVIEW_CODES)
            & (d["time"].between(*M12_WINDOW))
            & (_num(d, "f_s1") == 1)
            & (ob >= 0.55)
            & (_num(d, "f_prior_vix_return") > -2.0)
            & (lb >= 0.45)
            & (_num(d, "score") >= 70.0)).fillna(False)


def timing(d: pd.DataFrame, model: str) -> dict:
    age = _num(d, "signal_age_min").dropna()
    fresh = age <= AGE_LIMIT_MIN
    out = {
        "rows": int(len(age)),
        "median_age_min": round(float(age.median()), 2) if len(age) else None,
        "p90_age_min": round(float(age.quantile(0.9)), 2) if len(age) else None,
        "pct_over_limit": round(float(100 * (~fresh).mean()), 1) if len(age) else None,
        "qualified_all_gates": 0,
        "qualified_but_stale": 0,
        "qualified_and_fresh": 0,
        "bars_with_signals": int(d["time"].nunique()) if "time" in d else 0,
        "bars_reachable_in_window": 0,
        "symbols_ever_fresh": 0,
    }
    if "time" in d and "symbol" in d:
        ok = and_all(d, model)
        freshmask = _num(d, "signal_age_min") <= AGE_LIMIT_MIN
        out["qualified_all_gates"] = int(ok.sum())
        out["qualified_but_stale"] = int((ok & ~freshmask.fillna(False)).sum())
        out["qualified_and_fresh"] = int((ok & freshmask.fillna(False)).sum())
        out["bars_reachable_in_window"] = int(d.loc[freshmask.fillna(False), "time"].nunique())
        out["symbols_ever_fresh"] = int(d.loc[freshmask.fillna(False), "symbol"].nunique())
    return out


def latest_ledger_session() -> str | None:
    """Most recent session present in any M12/M13 ledger - the audit clock."""
    days = [os.path.basename(f).split("_")[-1][:10]
            for f in glob.glob(str(ROOT / "learn" / "*_candidates_*.csv"))
            if os.path.getsize(f) > 16]
    return max(days) if days else None


def prior_trading_day(d) -> str:
    """Same rollback the models use: step back over weekends only."""
    q = pd.Timestamp(d) - pd.Timedelta(days=1)
    while q.weekday() >= 5:
        q -= pd.Timedelta(days=1)
    return q.strftime("%Y-%m-%d")


def history_freshness(day: str | None) -> dict:
    """Staleness clock: --day when given, otherwise the newest audited ledger session."""
    files = sorted(glob.glob(str(ROOT / "data" / "history" / "*.csv")))
    last_days = Counter()
    rel = {}
    for fp in files[:400]:
        try:
            with open(fp) as f:
                f.readline()
                rows = f.read().strip().splitlines()
        except Exception:
            continue
        if not rows:
            continue
        stamp = rows[-1].split(",")[0][:10]
        last_days[stamp] += 1
        if Path(fp).stem == "RELIANCE":
            parts = rows[-1].split(",")
            try:
                rel = {"dt": parts[0], "close": float(parts[4])}
            except (IndexError, ValueError):
                rel = {"dt": parts[0], "close": None}
    common = last_days.most_common(1)[0][0] if last_days else None
    return {
        "history_files": len(files),
        "last_session_in_history": common,
        "files_at_that_date": last_days.most_common(1)[0][1] if last_days else 0,
        "sample_tail_bar": rel,
        "expected_previous_weekday": (prior_trading_day(day or latest_ledger_session() or common)
                                      if common else None),
        "stale_by_days": ((pd.Timestamp(day or latest_ledger_session() or common) - pd.Timestamp(common)).days
                          if common else None),
    }


def cache_freshness() -> dict:
    out = {}
    for name, path in (("m12", "data/m12_prev_close.json"), ("m13", "data/m13_prev_context.json")):
        fp = ROOT / path
        if not fp.exists():
            out[name] = {"present": False}
            continue
        j = json.loads(fp.read_text())
        closes = j.get("close") or {}
        out[name] = {
            "present": True,
            "date": j.get("date"),
            "count": len(closes),
            "meets_180_coverage": len(closes) >= MIN_PREV_COVERAGE,
            "symbols": sorted(closes)[:6],
            "last_bar_min": j.get("last_bar_min"),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=None, help="limit to one session (YYYY-MM-DD)")
    ap.add_argument("--json", default=None, help="also dump machine-readable results")
    a = ap.parse_args()
    report: dict = {"day": a.day}
    blocking: list[str] = []

    print("=" * 78)
    print("M12 / M13 ENTRY FUNNEL, TIMING AND DATA-FRESHNESS AUDIT")
    print("=" * 78)

    for model, title, fn in (("m12", "M12 Selective Reversion", funnel_m12),
                             ("m13", "M13 Equity Momentum A+", funnel_m13)):
        d = load_ledgers(model, a.day)
        print(f"\n### {title}  ·  {len(d)} recorded candidates"
              f"{' (day ' + a.day + ')' if a.day else ' (all committed sessions)'}")
        if d.empty:
            print("   no ledger rows")
            continue
        for label, npass, ntot in fn(d):
            print(f"   {label:52s} {npass:5d}/{ntot:<5d} ({100*npass/ntot:5.1f}%)")
        t = timing(d, model)
        print(f"   {'--- timing (bar close + 5 min deadline) ---':52s}")
        print(f"   median signal age {t['median_age_min']} min · p90 {t['p90_age_min']} min · "
              f"{t['pct_over_limit']}% over the {AGE_LIMIT_MIN}-min limit")
        print(f"   passed EVERY hard gate: {t['qualified_all_gates']}   "
              f"of which rejected only for staleness: {t['qualified_but_stale']}   "
              f"actionable (fresh): {t['qualified_and_fresh']}")
        print(f"   session coverage: {t['bars_reachable_in_window']}/{t['bars_with_signals']} "
              f"signal-bearing bars reachable in-window · "
              f"{t['symbols_ever_fresh']}/210 symbols ever evaluated fresh")
        if t["qualified_all_gates"] and t["qualified_and_fresh"] == 0:
            blocking.append(f"{model.upper()}: {t['qualified_all_gates']} fully-qualified signals "
                            f"were all discarded by the {AGE_LIMIT_MIN}-minute freshness rule")
        report[model] = t

    print("\n### Data freshness (previous-close baseline for both models)")
    hf = history_freshness(a.day)
    print(f"   data/history files: {hf['history_files']} · "
          f"latest session present: {hf['last_session_in_history']} "
          f"({hf['files_at_that_date']} files) · history is {hf['stale_by_days']} days behind the "
          f"newest audited session; correct baseline would be {hf['expected_previous_weekday']}")
    if hf["sample_tail_bar"]:
        print(f"   RELIANCE tail bar: {hf['sample_tail_bar']['dt']} @ {hf['sample_tail_bar']['close']}")
    cf = cache_freshness()
    for name, v in cf.items():
        print(f"   {name} prev-context cache: {v}")
        if v.get("present") and not v.get("meets_180_coverage"):
            blocking.append(f"{name.upper()}: prev-close cache covers only {v['count']} symbols "
                            f"(< {MIN_PREV_COVERAGE}) so seeding never succeeds")
    for state in ("state12.json", "state13.json"):
        fp = ROOT / state
        if not fp.exists():
            continue
        st = json.loads(fp.read_text())
        meta = st.get("prev_meta") or {}
        alerts = st.get("alerts") or []
        entries = [x for x in alerts if x.endswith(":ENTRY")]
        print(f"   {state}: date={st.get('date')} cycles={st.get('cycles')} "
              f"trades={len(st.get('trades', {}))} decisions={len(st.get('decisions', []))}")
        print(f"        prev_meta={meta}")
        print(f"        alerts={len(alerts)} (ENTRY alerts={len(entries)})  feed={st.get('feed')}")
        if meta.get("status") == "OK" and meta.get("date") and meta.get("date") != meta.get("expected_previous_weekday") \
                and meta.get("expected_previous_weekday"):
            blocking.append(f"{state}: previous-close baseline reported OK but is "
                            f"{meta.get('date')} instead of {meta.get('expected_previous_weekday')}")
        if len(entries) == 0:
            blocking.append(f"{state}: zero ENTRY alerts reserved today — "
                            f"only the EOD pair was sent")

    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1, default=str))
        print(f"\nwrote {a.json}")

    print("\n" + "=" * 78)
    if blocking:
        print("BLOCKING FINDINGS:")
        for b in blocking:
            print("  ✗ " + b)
    else:
        print("no blocking findings")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
