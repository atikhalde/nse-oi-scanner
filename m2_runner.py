#!/usr/bin/env python3
"""MODEL 2 paper trader — master signal × (NSE OI-spurts, ANY rank) ×
(own-computed F&O Top-20 Gainers ∪ Top-20 Losers).

Gate (user-confirmed):
  1. stock present ANYWHERE on NSE's live OI-spurts list (any %OI rise), AND
  2. stock in today's Top-20 Gainers OR Top-20 Losers among the 210 FnO names,
     ranked by (last price - prev close) % — computed natively from our own
     feeds + committed history (same universe as NSE's 'F&O stocks' tab).

Rules / engine / sizing: identical to Model 1 (intact master engine, pivot SL
∓0.02%, 50% @1:2, trail after 1:3, 15:20 square-off, ₹50,000 per stock).

Separate ledger/state (state2.json) + separate workflow (4. LIVE M2).
Model 1 (live_runner.py) is untouched. Alerts are entry/exit only, tagged 🅼2.
Usage: python -u m2_runner.py [--loop N]
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

import pandas as pd

import live_runner as L                  # ROOT, SYMS, SID, HIST, MASTER_CODES, engine_frame, now_ist, save helpers
import gate, feeds, trader, report
import telegram_bot as tg

STATE2 = L.ROOT / "state2.json"


def load_state(today):
    if STATE2.exists():
        st = json.loads(STATE2.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE2.write_text(json.dumps(st, indent=1))


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
        print("M2: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M2: pre-market — idle.")
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

    # --- engine -> new master signals -> M2 gate -> paper entry
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    for sym, tbars in bars_map.items():
        try:
            df = L.engine_frame(L.HIST / f"{sym}.csv", tbars, today)
            res = L.ms.run_symbol(df, params)
        except Exception as e:
            print(f"  engine {sym}: {e}")
            continue
        today_res = res[res.index.strftime("%Y-%m-%d") == today]
        known = st["signals"].get(sym, {}).get("nbars", 0)
        fired = False
        try:
            for idx, row in today_res.iloc[known:].iterrows():
                code = row.get("scan_code")
                if pd.isna(code) or int(code) not in L.MASTER_CODES:
                    continue
                side = "BUY" if int(code) < 200 else "SELL"
                etime = idx.strftime("%H:%M")
                px = df["close"].loc[idx]
                entry = float(px.iloc[-1] if hasattr(px, "iloc") else px)
                rank, in_mv = ranks.get(sym), sym in movers
                if not gate_ok:
                    print(f"  M2 {sym} {side} @ {etime} — SPURTS FEED OFFLINE: logged (strict)")
                    fired = True; break
                if rank is None or not in_mv:
                    print(f"  M2 {sym} {side} @ {etime} — filtered "
                          f"(spurt:{rank if rank else '-'} movers:{'Y' if in_mv else 'N'})")
                    fired = True; break
                tr = trader.evaluate(sym, side, etime, entry, str(row.get("scan_name", code)), tbars)
                tr["spurt_rank"] = int(rank)
                tr["movers20"] = True
                st["trades"][sym] = tr
                st["alerts"].append(f"{sym}:ENTRY")
                tg.send_message("🅼2 · " + trader.fmt_alert(tr, "ENTRY")
                                + f"\n🌊 OI-spurt rank #{rank} · F&O movers top-20 ✓")
                entries_now += 1
                print(f"  >>> M2 ENTRY {sym} {side} @ {entry} (spurt {rank}, movers ✓)")
                fired = True; break
        except Exception as e:
            print(f"  signals {sym}: {type(e).__name__}: {e}")
        st["signals"][sym] = st["signals"].get(sym, {})
        st["signals"][sym]["nbars"] = len(today_res)

    # --- manage open M2 trades (alert only NEW events)
    for sym in list(st["trades"].keys()):
        tbars = bars_map.get(sym)
        if tbars is None:
            continue
        tr = st["trades"][sym]
        try:
            new_tr = trader.evaluate(sym, tr["side"], tr["time"], float(tr["entry"]), tr["signal"], tbars)
            new_tr["spurt_rank"] = tr.get("spurt_rank")
            st["trades"][sym] = new_tr
            for ev in new_tr["events"]:
                key = f"{sym}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    tg.send_message("🅼2 · " + trader.fmt_alert(new_tr, ev["key"]))
                    st["alerts"].append(key)
        except Exception as e:
            print(f"  manage {sym}: {type(e).__name__}: {e}")

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M2: spurts-any × movers-20)"
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M2_{today}.xlsx"))
            tg.send_message("🅼2 EOD · " + report.summary_text(done, dlbl, st["gate"]))
            tg.send_document(out, caption=f"🅼2 📄 paper test report {today}")
            st["eod_done"] = True
        except Exception as e:
            print(f"  M2 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status (proof of life, 🅼2-tagged)
    if "09:20" <= hhmm < "15:26":
        tg.send_message(f"💓 🅼2 {hhmm} IST · {len(st['trades'])} trades · "
                        f"spurts {'OK' if gate_ok else 'OFFLINE'} · movers {meta_mv['count']}",
                        silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M2 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · "
          f"entries+{entries_now} · spurts {'OK' if gate_ok else 'OFFLINE'}")
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
            print(f"--- M2 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
