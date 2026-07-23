#!/usr/bin/env python3
"""MODEL 4 paper trader — OPTIONS BUYING mirror of m2_runner (M2).

Entries: IDENTICAL to M2 — intact master engine × NSE OI-spurts (any rank) ×
own-computed F&O Top-20 Gainers/Losers, with the same 09:26 gate + 90/290 block
+ B2 quality filter chain as M1/M3.

Position: nearest-expiry 1st-ITM option BUY (CE for BUY, PE for SELL), rules
identical to M3 (see options_common.py): SL prem computed once (delta × UL
signal-bar risk ∓0.02%), skip if 1-lot risk > ₹2,000, outlay ≤ ₹50,000,
TP1 1:1 book 50%, rest trails UL structure swings, sq-off 15:20.

Separate ledger (state4.json) + workflow (6. LIVE M4). Alerts tagged 🅼4.
Usage: python -u m4_runner.py --loop 2
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import pandas as pd

import live_runner as L                  # ROOT, SYMS, SID, HIST, MASTER_CODES, engine_frame, now_ist, save helpers
import options_common as optx   # options paper engine (M4)
import gate, feeds, report
import telegram_bot as tg

STATE4 = L.ROOT / "state4.json"


def load_state(today):
    if STATE4.exists():
        st = json.loads(STATE4.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE4.write_text(json.dumps(st, indent=1))


def prev_closes(symbols):
    """Previous-session close per symbol = last close on its committed history CSV."""
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
    """-> (movers:set, meta:dict). Rank 210 FnO by %chg vs prev close; top 20 each side."""
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


def mode_live():
    """One M2 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M4: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M4: pre-market — idle.")
        save_state(st); return False

    # --- gate leg 1: NSE live OI-spurts (strict: feed must be reachable)
    ranks, meta_sp = gate.nse_live(L.SYMS)
    spurts_ok = bool(ranks)
    print(f"M2 spurts: {meta_sp}")

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

    # --- gate leg 2: own F&O movers ranking (never external-offline)
    movers, meta_mv = movers_top20(bars_map, prev_closes(L.SYMS))
    print(f"M2 movers: {meta_mv}")
    st["gate"] = {"spurts": meta_sp, "movers": meta_mv}
    gate_ok = spurts_ok                                    # strict on the live spurts feed

    # --- manage open M2 trades FIRST (alert only NEW events).
    # Must run BEFORE the signal scan: with the 1-open-trade rule the scanner
    # needs 'closed' computed against the newest bars, so a SL/target that
    # printed on a bar frees the stock for a signal evaluated on that same bar.
    for tkey in list(st["trades"].keys()):
        sym = tkey.split("#")[0]
        tbars = bars_map.get(sym)
        if tbars is None:
            continue
        tr = st["trades"][tkey]
        try:
            if tr.get("closed"):
                continue                            # final; saves option-bar API calls
            try:
                obars = optx.today_opt_bars(tr, now)
            except Exception:
                obars = None
            new_tr = optx.evaluate_opt(tr, tbars, obars)
            new_tr["spurt_rank"] = tr.get("spurt_rank")
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    tg.send_message(optx.fmt_opt_alert(new_tr, ev["key"], tag="🅼4"))
                    st["alerts"].append(key)
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> new master signals -> M2 gate -> paper entry
    # CAUSAL evaluation (same as M1): each pending today-bar is evaluated ONLY
    # against data up to that bar's own close (engine re-run on truncated frame)
    # -> exactly TradingView's once-per-bar-close truth; no future-data leaks.
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    skipped_open = 0
    skipped_early = 0
    skipped_table = 0
    skipped_quality = 0
    for sym, tbars in bars_map.items():
        n_today = len(tbars)
        known = int(st["signals"].get(sym, {}).get("nbars", 0))
        if known > n_today:
            known = 0
        for j in range(known, n_today):
            t_bar = tbars["dt"].iloc[j]
            try:
                tk = pd.Timestamp(t_bar)                      # forming-bar guard
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
                    entry = float(tbars["close"].iloc[j])     # signal-bar close
                    rank, in_mv = ranks.get(sym), sym in movers
                    if int(code) in (90, 290):
                        print(f"  M4 {sym} {side} @ {etime} — SKIPPED: scanner-table preview 90/290 (no chart label)")
                        tg.send_message(L.fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                      tag="🅼4 · ",
                                                      why="scanner-table preview (ENTRY BUY/SELL) — appears only in the TradingView scanner table, never as a chart label"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "scanner-table preview signal (90/290) — no TradingView chart label, blocked"})
                        skipped_table += 1
                        time.sleep(0.5)
                    elif int(code) in L.EX_WEAK_CODES:
                        print(f"  M4 {sym} {side} @ {etime} — SKIPPED: weak EX variant (EX9+) quality filter")
                        tg.send_message(L.fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                      tag="🅼4 · ",
                                                      why="weak exception variant (EX9 or higher) — quality filter: EX9+ lost money in the 2-day review; only EX1-EX8 + NORMAL signals are traded"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "weak EX variant (EX9+) blocked — B2 quality filter"})
                        skipped_quality += 1
                        time.sleep(0.5)
                    elif int(code) in L.EX_RAZOR_CODES and etime < L.EX_OPEN_FROM:
                        print(f"  M4 {sym} {side} @ {etime} — SKIPPED: EX1/EX2 at open (razor class, allowed from {L.EX_OPEN_FROM})")
                        tg.send_message(L.fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                      tag="🅼4 · ",
                                                      why=f"fresh EX1/EX2 at the open — razor-edge class (Dhan-vs-TradingView tick flips; biggest loser group in review); allowed from {L.EX_OPEN_FROM}"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": f"EX1/EX2 before {L.EX_OPEN_FROM} blocked — razor-edge class (B2)"})
                        skipped_quality += 1
                        time.sleep(0.5)
                    elif etime < L.CHART_MIN_TIME:
                        print(f"  M4 {sym} {side} @ {etime} — SKIPPED: chart window (<{L.CHART_MIN_TIME})")
                        tg.send_message(L.fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                      tag="🅼4 · ",
                                                      why=f"before {L.CHART_MIN_TIME} chart window — not on TradingView yet"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": f"signal before {L.CHART_MIN_TIME} chart window (not on TradingView)"})
                        skipped_early += 1
                        time.sleep(0.5)
                    elif not gate_ok:
                        print(f"  M4 {sym} {side} @ {etime} — SPURTS FEED OFFLINE: logged (strict)")
                    elif rank is None or not in_mv:
                        print(f"  M4 {sym} {side} @ {etime} — filtered "
                              f"(spurt:{rank if rank else '-'} movers:{'Y' if in_mv else 'N'})")
                    elif L.sym_has_open(st, sym):
                        print(f"  M4 {sym} {side} @ {etime} — SKIPPED: {sym} trade already open (1-open-trade rule)")
                        tg.send_message(L.fmt_skipped(sym, side, str(row.get("scan_name", code)),
                                                      etime, entry, tag="🅼4 · "))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "open position already on stock (1-open-trade rule)"})
                        skipped_open += 1
                        time.sleep(0.5)
                    else:
                        tr = optx.enter(sym, side, etime, entry, str(row.get("scan_name", code)), tbars, now, sid=L.SID.get(sym), hist_csv=L.HIST / f"{sym}.csv")
                        if "error" in tr:
                            print(f"  M4 {sym} {side} @ {etime} — options rejected: {tr.get('error')}")
                        else:
                            tr["spurt_rank"] = int(rank)
                            tr["movers20"] = True
                            tkey, k = sym, 2
                            while tkey in st["trades"]:
                                tkey = f"{sym}#{k}"; k += 1
                            st["trades"][tkey] = tr
                            st["alerts"].append(f"{tkey}:ENTRY")
                            suffix = f" · #{k-1} on {sym}" if tkey != sym else ""
                            tg.send_message(optx.fmt_opt_alert(tr, "ENTRY", tag="🅼4")
                                            + f"\n🌊 OI-spurt rank #{rank} · F&O movers top-20 ✓{suffix}")
                            entries_now += 1
                            print(f"  >>> M2 ENTRY {tkey} {side} @ {entry} (spurt {rank}, movers ✓)")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")
            st["signals"][sym] = st["signals"].get(sym, {})
            st["signals"][sym]["nbars"] = j + 1

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M4: OPTIONS spurts-any × movers-20)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append([it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M4_{today}.xlsx"), skipped=sk or None, rules_note=optx.OPT_RULES_NOTE)
            tg.send_message("🅼4 EOD · " + report.summary_text(done, dlbl, st["gate"]))
            tg.send_document(out, caption=f"🅼4 📄 OPTIONS paper report {today}")
            st["eod_done"] = True
        except Exception as e:
            print(f"  M4 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status (proof of life, 🅼4-tagged)
    if "09:20" <= hhmm < "15:26":
        tg.send_message(f"💓 🅼4 {hhmm} IST · {len(st['trades'])} trades · "
                        f"spurts {'OK' if gate_ok else 'OFFLINE'} · movers {meta_mv['count']}",
                        silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M4 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · "
          f"entries+{entries_now} · skipped-open +{skipped_open} · skip-<09:26 +{skipped_early} · skip-table +{skipped_table} · skip-qual +{skipped_quality} · spurts {'OK' if gate_ok else 'OFFLINE'}")
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
            print(f"--- M4 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
