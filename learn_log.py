#!/usr/bin/env python3
"""Phase-0 LEARNING LOG — dataset engine for the future ML entry engine (user pick A, 25-Jul-2026).

The runners already journal everything in their states (taken trades, skipped candidates
with block reasons, ghost shadow outcomes). This module harvests that state ONCE per day
at EOD into learn/raw_{MODEL}_{DAY}.csv — one row per candidate decision with context
features. label_learn.py (bootstrap job, next morning) adds outcome labels.

Contract: NEVER break trading. Every public function swallows its own exceptions and
only prints. Zero behavior change: called only from inside the existing EOD report block.

Columns (raw):
  day, model, sym, side, sig, code, time, min, entry, rank, cls, taken, ghost, skip_why,
  breadth, daypct, sec, sec_bullq, sec_bearq, sec_pb, sec_pw, held, tilt, regime
Ghost rows additionally carry cf-net/r/exit per stop style (evaluated by run_ghosts).
"""
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
HIST = ROOT / "data" / "history"
LEARN = ROOT / "learn"

COLS = ["day", "model", "sym", "side", "sig", "code", "time", "min", "entry", "rank",
        "cls", "taken", "ghost", "skip_why", "breadth", "daypct", "sec",
        "sec_bullq", "sec_bearq", "sec_pb", "sec_pw", "held", "tilt", "regime",
        "cf_net_s", "cf_net_p", "cf_r_s", "cf_r_p", "cf_exit_s"]

_SECMAP = None
_PC = {}


def code_of(sig, side):
    """signal name → numeric code (mirrors the scanner code↔name map used in replay)."""
    try:
        if sig in ("ENTRY BUY", "ENTRY SELL"):
            return 90 if side == "BUY" else 290
        if sig in ("NORMAL BUY", "NORMAL SELL"):
            return 80 if side == "BUY" else 280
        if sig == "BUY-EX":
            return 101
        if sig == "SELL-EX":
            return 201
        n = int(re.search(r"EX(\d+)", str(sig)).group(1))
        return (200 + n) if side == "SELL" else (102 if n == 17 else 99 + n)
    except Exception:
        return ""


def _secmap():
    global _SECMAP
    if _SECMAP is None:
        try:
            _SECMAP = dict(pd.read_csv(ROOT / "fno_sector_map.csv").values)
        except Exception:
            _SECMAP = {}
    return _SECMAP


def prev_close(sym, today):
    """previous session's last close from committed history (cached)."""
    key = (sym, today)
    if key not in _PC:
        pc = ""
        try:
            fp = HIST / f"{sym}.csv"
            if fp.exists():
                h = pd.read_csv(fp, parse_dates=["dt"])
                d = h["dt"].dt.strftime("%Y-%m-%d")
                h = h[d < today]
                if len(h):
                    pc = round(float(h["close"].iloc[-1]), 4)
        except Exception:
            pc = ""
        _PC[key] = pc
    return _PC[key]


def breadth_below_open(bars_map):
    """% of stocks with bars whose last close is below their day open (at call time)."""
    try:
        dn = up = 0
        for b in (bars_map or {}).values():
            if b is None or len(b) == 0:
                continue
            if float(b["close"].iloc[-1]) < float(b["open"].iloc[0]):
                dn += 1
            else:
                up += 1
        return round(dn / max(1, dn + up), 3)
    except Exception:
        return ""


def _mins(t):
    try:
        return int(t[:2]) * 60 + int(t[3:5])
    except Exception:
        return ""


def harvest(model, today, st, ghosts, bars_map, board=None, extra=None):
    """One-call journal: dump today's decisions from the runner state into the learn CSV."""
    try:
        rows = []
        br = breadth_below_open(bars_map)
        smap = _secmap()
        extra = extra or {}

        def feat(sym, side, time_, entry):
            pc = prev_close(sym, today)
            dp = ""
            try:
                if pc not in ("", None) and float(pc) > 0:
                    dp = round((float(entry) / float(pc) - 1) * 100, 3)
            except Exception:
                pass
            sec = smap.get(sym, "")
            out = dict(breadth=br, daypct=dp, sec=sec)
            if board:
                secs = board.get("sectors", {})
                out.update(sec_bullq=int(bool(sec and any(sec == n for n, _ in board.get("bull_q", [])))),
                           sec_bearq=int(bool(sec and any(sec == n for n, _ in board.get("bear_q", [])))),
                           sec_pb=int(bool(sec and any(sec == n for n, _ in board.get("persist_bull", [])))),
                           sec_pw=int(bool(sec and any(sec == n for n, _ in board.get("persist_bear", [])))),
                           held=int(bool(sec and (sec in (board.get("held_bull") or [])
                                                  or sec in (board.get("held_bear") or [])))),
                           tilt=board.get("net_tilt", ""))
            return out

        base = dict(day=today, model=model)
        for tr in (st.get("trades") or {}).values():
            if not isinstance(tr, dict) or "symbol" not in tr:
                continue
            r = dict(base, sym=tr.get("symbol"), side=tr.get("side"), sig=tr.get("signal"),
                     code=code_of(tr.get("signal"), tr.get("side")), time=tr.get("time"),
                     min=_mins(tr.get("time", "")), entry=tr.get("entry"),
                     rank=tr.get("gate_rank", tr.get("rank", "")), cls=tr.get("setup", ""),
                     taken=1, ghost=0, skip_why="",
                     cf_net_s=tr.get("pnl", ""), cf_net_p="", cf_r_s=tr.get("r_total", ""),
                     cf_r_p="", cf_exit_s=tr.get("exit_text", ""))
            r.update(feat(tr.get("symbol"), tr.get("side"), tr.get("time"), tr.get("entry")))
            r.update(extra)
            rows.append(r)
        for it in (st.get("skipped") or []):
            r = dict(base, sym=it.get("symbol"), side=it.get("side"), sig=it.get("signal"),
                     code=code_of(it.get("signal"), it.get("side")), time=it.get("time"),
                     min=_mins(it.get("time", "")), entry=it.get("entry"),
                     rank=it.get("rank", ""), cls="", taken=0, ghost=0,
                     skip_why=str(it.get("why", ""))[:80],
                     cf_net_s="", cf_net_p="", cf_r_s="", cf_r_p="", cf_exit_s="")
            r.update(feat(it.get("symbol"), it.get("side"), it.get("time"), it.get("entry")))
            r.update(extra)
            rows.append(r)
        for g in (ghosts or []):
            r = dict(base, sym=g.get("symbol"), side=g.get("side"), sig=g.get("signal"),
                     code=g.get("code", code_of(g.get("signal"), g.get("side"))), time=g.get("time"),
                     min=_mins(g.get("time", "")), entry=g.get("entry"),
                     rank=g.get("rank", ""), cls="", taken=0, ghost=1, skip_why="",
                     cf_net_s=g.get("net_structure"), cf_net_p=g.get("net_prevbar"),
                     cf_r_s=g.get("r_structure"), cf_r_p=g.get("r_prevbar"),
                     cf_exit_s=g.get("exit_structure", ""))
            r.update(feat(g.get("symbol"), g.get("side"), g.get("time"), g.get("entry")))
            r.update(extra)
            rows.append(r)

        if rows:
            LEARN.mkdir(exist_ok=True)
            df = pd.DataFrame(rows)
            for c in COLS:
                if c not in df.columns:
                    df[c] = ""
            df = df[COLS + [c for c in df.columns if c not in COLS]]
            fp = LEARN / f"raw_{model}_{today}.csv"
            df.to_csv(fp, index=False)
            print(f"  learn_log: {len(df)} rows → {fp.name} "
                  f"(taken {int(df['taken'].sum())}, ghosts {int(df['ghost'].sum())})")
    except Exception as e:
        print(f"  learn_log.harvest[{model}]: {type(e).__name__}: {e}")
