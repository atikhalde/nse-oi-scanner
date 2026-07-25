#!/usr/bin/env python3
"""MODEL 7 — RAW CONTROL paper trader (25-Jul-2026, user pick A).

The system's CONTROL ARM and data farm: take EVERY real master signal — no quality
gates, no sector gate, no movers filter, no breadth filter, no leader test. Its job
is NOT profit — it is the unbiased measuring stick that (a) feeds the Phase-0
Learning Log / ML engine the full raw signal universe, and (b) lets us A/B every gate
in M1/M2/M5/M6 against "do nothing" on identical signals.

Exits (user spec): structure SL ∓0.02%, NO trail (TRAIL_ARM_R disarmed below), NO
targets, 15:20 EOD square-off. Sizing identical (₹50k notional / ₹900 risk cap).
1-open-trade-per-symbol rule kept (else one trend = N copies of the same bet = fake
sample size). ONLY blocks kept — the two "not-a-real-signal" ones every model shares:
90/290 scanner-table previews (no TradingView chart label) + pre-09:26 chart window.

ALERTS: entries and exit legs SILENT (data farm — keep the signal channel clean);
silent heartbeat each cycle + loud EOD xlsx report. Ledger state7.json · tag 🅼7 ·
workflow "7. LIVE M7". Joins the Learning Log automatically as model M7.
Usage: python -u m7_runner.py [--loop N]
"""
import argparse
import datetime as dt
import json
import sys
import time

import pandas as pd

import live_runner as L                  # engine, universe, engine_frame
import learn_log
import feeds, trader, report
import telegram_bot as tg

STATE7 = L.ROOT / "state7.json"

# M7 exits policy: disarm the +1R trailing engine → positions exit ONLY at structure
# SL or the 15:20 EOD square-off (structure SL ∓0.02%, no targets — trader defaults).
trader.TRAIL_ARM_R = 99.0


def load_state(today):
    if STATE7.exists():
        st = json.loads(STATE7.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE7.write_text(json.dumps(st, indent=1))


def mode_live():
    """One M7 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M7: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M7: pre-market — idle.")
        save_state(st); return False

    # --- fetch today's stock bars (needed for the scanner)
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

    # --- raw control: NO boards, NO gates — every real chart signal trades
    st["gate"] = {"status": "RAW",
                  "source": "no gate (control arm): every master signal · structure SL or 15:20 EOD"}

    # --- manage open trades FIRST
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
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    print(f"  M7 exit (silent) {sym} {ev['key']}")
                    st["alerts"].append(key)
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> master signals -> NO GATE -> paper entry (causal, once per bar close)
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    skipped_phantom = 0
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
                    name = str(row.get("scan_name", code))
                    why = None
                    if int(code) in (90, 290):
                        why = "scanner-table preview (90/290) — no chart label"
                    elif etime < L.CHART_MIN_TIME:
                        why = f"before {L.CHART_MIN_TIME} chart window"
                    elif L.sym_has_open(st, sym):
                        why = "open position already on stock (1-open-trade rule)"
                    if why:
                        print(f"  M7 {sym} {side} {name} @ {etime} — SKIPPED (phantom): {why}")
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": name, "time": etime,
                             "entry": round(entry, 2), "why": why})
                        skipped_phantom += 1
                        continue
                    tr = trader.evaluate(sym, side, etime, entry, name, tbars,
                                         warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today))
                    if "error" in tr:
                        print(f"  M7 {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                        continue
                    tkey, k = sym, 2
                    while tkey in st["trades"]:
                        tkey = f"{sym}#{k}"; k += 1
                    st["trades"][tkey] = tr
                    st["alerts"].append(f"{tkey}:ENTRY")
                    entries_now += 1
                    print(f"  >>> M7 ENTRY (silent) {tkey} {side} @ {entry} qty {tr['qty']} · "
                          f"SL {tr['sl']} (SL-or-15:20 EOD, no trail)")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")
            st["signals"][sym] = st["signals"].get(sym, {})
            st["signals"][sym]["nbars"] = j + 1

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M7: raw control)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append(
                    [it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M7_{today}.xlsx"),
                               skipped=sk or None)
            learn_log.harvest("M7", today, st, None, bars_map)
            msg = ("🅼7 EOD · " + report.summary_text(done, dlbl, st["gate"])
                   + "\n(raw control — data arm, not a profit model)")
            tg.send_message(msg)
            tg.send_document(out, caption=f"🅼7 📄 M7 raw-control paper report {today}")
            st["eod_done"] = True
        except Exception as e:
            print(f"  M7 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status
    if "09:20" <= hhmm < "15:26":
        tg.send_message(f"💓 🅼7 {hhmm} IST · {len(st['trades'])} trades · RAW control (no filters)",
                        silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M7 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · "
          f"entries+{entries_now} · phantom-skips {skipped_phantom}")
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
            print(f"--- M7 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
