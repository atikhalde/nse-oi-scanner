#!/usr/bin/env python3
"""MODEL 5 — WINNER-FINGERPRINT LAB (paper-only experiment, 24-Jul-2026).

Built from the 3-day winners/regime study (REPORT_WINNERS_3DAYS.md). Takes the
M2 gate (best cohort: OI-spurts ANY rank × own F&O top/bottom-20 movers) and adds
the measured big-winner factors:

  1. TIME rule : entries <= 11:00 free (morning trend leg, where all 8 big
                 winners entered). After 11:00 an entry is allowed ONLY with the
                 breadth majority on its side (>=55% of the universe below day-open
                 for SELL / <=45% for BUY) — the trap-day reversal guard.
  2. SETUP rule: BREAKOUT entries only (all 8 big winners were BREAKOUT at entry;
                 PULLBACK/CONTINUATION/REVERSAL signals are logged + skipped).

Everything else is identical to the shipped stack: intact master engine, B2 blocks
(90/290, weak-EX with GHOST shadow, razor pre-09:45), 09:26 chart window,
structure SL exits v3 (no targets, +1R trail), ₹50k/₹900 sizing, 15:20 sq-off,
1-open-trade-per-stock (no duplicate-idea risk inside a single ledger).

Honest offline 3-day score (same sequential frame, costs+slippage):
  M5: 41 trades, NET −₹2,843 (22-Jul +492 · 23-Jul +44 · 24-Jul −3,379)
  vs M1+M2 current same frame: 91 trades NET −₹2,585.
  → the fingerprint SAVES ~₹5.8k on the trap Friday but gives back ~₹6k of
    trend-day winners (movers cut clips fast winners at entry). NOT a free lunch;
    forward 10-session paper evidence will decide. Zero real-money exposure.

Separate ledger (state5.json) + separate workflow (5. LIVE M5) + tag 🅼5.
M1/M2 are untouched. Requires the ghost-shadow build of live_runner.py.
Usage: python -u m5_runner.py [--loop N]
"""
import argparse
import datetime as dt
import json
import sys
import time

import pandas as pd

import live_runner as L                  # engine, universe, engine_frame, run_ghosts, fmt_skipped
import learn_log
import gate, feeds, trader, report
import telegram_bot as tg

STATE5 = L.ROOT / "state5.json"
POST11_FROM = "11:00"                      # after this, entries need breadth majority
BREADTH_MAJ = 0.55                         # 55% of universe must agree with the side


def load_state(today):
    if STATE5.exists():
        st = json.loads(STATE5.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE5.write_text(json.dumps(st, indent=1))


def prev_closes(symbols):
    out = {}
    for sym in symbols:
        fp = L.HIST / f"{sym}.csv"
        try:
            if fp.exists():
                tail = pd.read_csv(fp).tail(1)
                if not tail.empty:
                    out[sym] = float(tail["close"].iloc[-1])
        except Exception:
            pass
    return out


def movers_top20(bars_map, pc):
    """Own F&O movers ranking (identical logic to M2): %chg vs prev close, 20 each side."""
    pct = []
    for sym, b in bars_map.items():
        base = pc.get(sym)
        if base and base > 0:
            last = float(b["close"].iloc[-1])
            pct.append((sym, (last - base) / base * 100.0))
    pct.sort(key=lambda x: -x[1])
    top = {s for s, _ in pct[:20]}
    bot = {s for s, _ in pct[-20:]}
    meta = {"count": len(pct),
            "top20_cut": round(pct[19][1], 2) if len(pct) >= 20 else None,
            "bot20_cut": round(pct[-20][1], 2) if len(pct) >= 20 else None}
    return top | bot, meta


def breadth_bearish(bars_map):
    """fraction of fetched universe with last close BELOW its own day-open."""
    dn = tot = 0
    for sym, b in bars_map.items():
        if b is None or b.empty:
            continue
        tot += 1
        if float(b["close"].iloc[-1]) < float(b["open"].iloc[0]):
            dn += 1
    return (dn / tot) if tot else 0.5


def regime_label(bars_map, pc):
    """EOD regime fingerprint: breadth path + green-close + median CLV + wide gaps."""
    checks = {"09:45": None, "10:30": None, "11:30": None, "13:00": None, "15:20": None}
    stats = {k: {"dn": 0, "n": 0} for k in checks}
    clvs, gaps, green = [], 0, 0
    n = 0
    for sym, b in bars_map.items():
        if b is None or len(b) < 10:
            continue
        n += 1
        o = float(b["open"].iloc[0]); c = float(b["close"].iloc[-1])
        hi = float(b["high"].max()); lo = float(b["low"].min())
        green += c > o
        clvs.append((c - lo) / (hi - lo) if hi > lo else 0.5)
        base = pc.get(sym)
        if base and abs(o - base) / base > 0.004:
            gaps += 1
        for k in checks:
            sub = b[b["t"] <= k]
            if len(sub) >= 2:
                stats[k]["n"] += 1
                stats[k]["dn"] += float(sub["close"].iloc[-1]) < o
    bpath = {k: (v["dn"] / v["n"] if v["n"] else None) for k, v in stats.items()}
    g = green / n if n else 0.5
    clv = round(sorted(clvs)[len(clvs) // 2], 2) if clvs else None
    b1030, bclose = bpath.get("10:30"), bpath.get("15:20")
    lbl = "MIXED/chop"
    if gaps >= 70 and (b1030 or 0) >= 0.55 and g >= 0.55:
        lbl = "V-REVERSAL / bear-trap"
    elif (b1030 or 0) >= 0.70 and (bclose or 0) >= 0.65:
        lbl = "TREND-DOWN day"
    elif (b1030 or 1) <= 0.30 and (bclose or 1) <= 0.35:
        lbl = "TREND-UP day"
    fmt = lambda x: f"{x*100:.0f}%" if x is not None else "-"
    return (f"🧭 Regime: breadth-below-open 09:45 {fmt(bpath['09:45'])} · 10:30 {fmt(bpath['10:30'])} · "
            f"11:30 {fmt(bpath['11:30'])} · 13:00 {fmt(bpath['13:00'])} · close {fmt(bpath['15:20'])} · "
            f"green-close {g*100:.0f}% · median CLV {clv} · wide gaps {gaps} → {lbl}"), lbl


def skip(st, sym, side, name, etime, entry, rank, in_mv, why):
    """Silent log-only skip (M5 lab: no Telegram noise on policy skips)."""
    print(f"  M5 {sym} {side} @ {etime} — SKIPPED: {why}")
    st.setdefault("skipped", []).append(
        {"symbol": sym, "side": side, "signal": str(name),
         "time": etime, "entry": round(entry, 2),
         "rank": (int(rank) if rank is not None else None), "mv": bool(in_mv), "why": why})


def mode_live():
    """One M5 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M5: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M5: pre-market — idle.")
        save_state(st); return False

    # --- gate leg 1: NSE live OI-spurts ANY rank (strict: feed must be reachable)
    ranks, meta_sp = gate.nse_live(L.SYMS)
    spurts_ok = bool(ranks)
    print(f"M5 spurts: {meta_sp}")

    # --- fetch today's bars (one bad feed never kills the cycle)
    bars_map = {}
    for sym in L.SYMS:
        try:
            b, _src = feeds.fetch_today(sym, L.SID[sym], now)
            if b is not None and not b.empty:
                b = b.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
                b["t"] = b["dt"].dt.strftime("%H:%M")
                bars_map[sym] = b
        except Exception as e:
            print(f"  feed {sym}: {type(e).__name__}: {e}")
        time.sleep(0.15)

    pc = prev_closes(L.SYMS)
    movers, meta_mv = movers_top20(bars_map, pc)
    br = breadth_bearish(bars_map)
    print(f"M5 movers: {meta_mv} · breadth-below-open {br*100:.0f}%")
    st["gate"] = {"spurts": meta_sp, "movers": meta_mv, "breadth_bearish": round(br, 3)}
    gate_ok = spurts_ok

    # --- manage open M5 trades FIRST (alert only NEW events)
    for tkey in list(st["trades"].keys()):
        sym = tkey.split("#")[0]
        tbars = bars_map.get(sym)
        if tbars is None:
            continue
        tr = st["trades"][tkey]
        try:
            new_tr = trader.evaluate(sym, tr["side"], tr["time"], float(tr["entry"]), tr["signal"], tbars,
                                     warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today),
                                     sl_mode=tr.get("sl_mode", "structure"))
            new_tr["spurt_rank"] = tr.get("spurt_rank")
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    tg.send_message("🅼5 · " + trader.fmt_alert(new_tr, ev["key"]))
                    st["alerts"].append(key)
                    save_state(st)          # persist alert registry instantly (no-repeat guarantee)
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> master signals -> M5 gate -> M5 rules -> paper entry
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    for sym, tbars in bars_map.items():
        n_today = len(tbars)
        known = int(st["signals"].get(sym, {}).get("nbars", 0))
        if known > n_today:
            known = 0
        for j in range(known, n_today):
            t_bar = tbars["dt"].iloc[j]
            try:
                tk = pd.Timestamp(t_bar)
                if tk.tzinfo is None:
                    tk = tk.tz_localize(now.tz)
                if tk + pd.Timedelta(minutes=5) > pd.Timestamp(now):
                    break
                df = L.engine_frame(L.HIST / f"{sym}.csv", tbars.iloc[: j + 1], today)
                res = L.ms.run_symbol(df, params)
                row = res.iloc[-1]
            except Exception as e:
                print(f"  engine {sym}: {e}")
                break
            try:
                code = row.get("scan_code")
                if not pd.isna(code) and int(code) in L.MASTER_CODES:
                    side = "BUY" if int(code) < 200 else "SELL"
                    etime = tk.strftime("%H:%M")
                    entry = float(tbars["close"].iloc[j])
                    rank, in_mv = ranks.get(sym), sym in movers
                    name = str(row.get("scan_name", code))
                    if int(code) in (90, 290):
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             "scanner-table preview (90/290) — no chart label, blocked")
                    elif int(code) in L.EX_WEAK_CODES:
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             "weak EX variant (EX9+) blocked — B2 (GHOST-shadowed EOD)")
                        if etime >= L.CHART_MIN_TIME:
                            st.setdefault("ghosts", []).append(
                                {"symbol": sym, "side": side, "signal": name,
                                 "code": int(code), "time": etime, "entry": round(entry, 2),
                                 "rank": (int(rank) if rank is not None else None),
                                 "mv": bool(in_mv),
                                 "grp": ("EX9" if int(code) == 108 else "R")})
                    elif int(code) in L.EX_RAZOR_CODES and etime < L.EX_OPEN_FROM:
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             f"EX1/EX2 at open blocked — razor class, allowed from {L.EX_OPEN_FROM}")
                    elif etime < L.CHART_MIN_TIME:
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             f"before {L.CHART_MIN_TIME} chart window")
                    elif not gate_ok:
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             "spurts feed offline (strict)")
                    elif rank is None or not in_mv:
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             f"M5 gate: spurt {rank if rank else '-'} · movers {'Y' if in_mv else 'N'} — need both")
                    elif L.sym_has_open(st, sym):
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             "open position already on stock (1-open-trade rule)")
                    elif etime > POST11_FROM and ((side == "SELL" and br < BREADTH_MAJ) or
                                                  (side == "BUY" and br > 1 - BREADTH_MAJ)):
                        skip(st, sym, side, name, etime, entry, rank, in_mv,
                             f"post-11:00 entry needs breadth majority on {side} side "
                             f"(below-open {br*100:.0f}% vs need {'≥55%' if side=='SELL' else '≤45%'}) — trap-day guard")
                    else:
                        tr = trader.evaluate(sym, side, etime, entry, name, tbars,
                                             warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today))
                        if "error" in tr:
                            skip(st, sym, side, name, etime, entry, rank, in_mv,
                                 f"trader rejected: {tr.get('error')}")
                        elif tr.get("setup") != "BREAKOUT":
                            skip(st, sym, side, name, etime, entry, rank, in_mv,
                                 f"setup filter: M5 takes BREAKOUT momentum only (got {tr.get('setup')})")
                        else:
                            tr["spurt_rank"] = int(rank)
                            tr["movers20"] = True
                            tkey, k = sym, 2
                            while tkey in st["trades"]:
                                tkey = f"{sym}#{k}"; k += 1
                            st["trades"][tkey] = tr
                            st["alerts"].append(f"{tkey}:ENTRY")
                            save_state(st)          # persist alert registry instantly (no-repeat guarantee)
                            suffix = f" · #{k-1} on {sym}" if tkey != sym else ""
                            tg.send_message("🅼5 · " + trader.fmt_alert(tr, "ENTRY")
                                            + f"\n🏆 spurt #{rank} · movers-20 ✓ · BREAKOUT-only · breadth {br*100:.0f}%{suffix}")
                            entries_now += 1
                            print(f"  >>> M5 ENTRY {tkey} {side} @ {entry} (spurt {rank}, movers ✓, BREAKOUT)")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")
            st["signals"][sym] = st["signals"].get(sym, {})
            st["signals"][sym]["nbars"] = j + 1

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M5: fingerprint lab)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append([it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            gh = L.run_ghosts(st, today, bars_map)
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M5_{today}.xlsx"),
                               skipped=sk or None, ghosts=gh or None)
            reg_line, lbl = regime_label(bars_map, pc)
            st["regime"] = lbl
            learn_log.harvest("M5", today, st, gh, bars_map, extra={"regime": st.get("regime")})
            tg.send_message("🅼5 EOD · " + report.summary_text(done, dlbl, st["gate"], ghosts=gh or None)
                            + "\n" + reg_line)
            tg.send_document(out, caption=f"🅼5 📄 M5 lab paper report {today}")
            st["eod_done"] = True
            save_state(st)          # so a crashed run never re-sends the EOD report
        except Exception as e:
            print(f"  M5 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status
    if "09:20" <= hhmm < "15:26":
        tg.send_message(f"💓 🅼5 {hhmm} IST · {len(st['trades'])} trades · "
                        f"spurts {'OK' if gate_ok else 'OFFLINE'} · breadth {br*100:.0f}% below-open",
                        silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M5 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · entries+{entries_now} · "
          f"spurts {'OK' if gate_ok else 'OFFLINE'} · breadth {br*100:.0f}%")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    a = ap.parse_args()
    for i in range(max(1, a.loop)):
        active = mode_live()
        if not active:
            break
        if i < a.loop - 1:
            print(f"--- M5 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
