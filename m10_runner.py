#!/usr/bin/env python3
"""MODEL 10 — COACH-v2.1 ENTRY LAB (31-Jul-2026, user directive "add breadth filter
— practical rules, should not be too strict").

Data-built entry model v2 (31-Jul, user vote A) + ONE breadth alignment filter v2.1
(user's design: "take only buy side entries if market is bullish, sell side if
bearish", softened per "practical rules, should not be too strict").

THE CARD:
  1. BEST-SETUP WHITELIST ONLY (~85% of raw flow blocked) — both sides:
       BUY : EX17(102) EX5(104) EX6(105) EX7(106) EX12(111)   (+0.27…+0.79R w1)
       SELL: EX8(208) EX12(212/213) EX19(219/220)             (+0.09…+0.15R w1)
     Blocked churn: plain BUY-EX(101, −0.057R on n=1,013), EX9, EX4, SELL-EX1
     (−0.266R), EX2, EX3, EX15, NORMAL SELL (−0.10R).
  2. BREADTH SIDE-GATE (v2.1 — live advance/decline over the 210 F&O stocks,
     computed from the SAME bars M10 already fetches + memoized prev closes; the
     frozen learn `breadth` column is ignored):
       advances ≥ 55%  → BULL : BUY side only (whitelisted + green-day confirm)
       advances ≤ 45%  → BEAR : SELL side only (whitelisted; BOTH fade + momentum
                                shorts allowed and tagged — w1 showed green-stock
                                SELL supply = ZERO on the 28-Jul bear day)
       45–55% (or breadth unknown) → NEUTRAL = permissive fallback: both sides,
                                current v2 confirms. Never hard-stops the lab.
       Anti-flap: regime flips only after 2 consecutive cycles past the line.
       SECTOR BREADTH (v2.2, user "only proven rules"): logged on every entry/skip
       (sector + sector adv% over the 210-stock universe, <3 fed members = None) —
       NO veto until forward data proves it (w1: +1.2R/n=7 — direction agrees,
       sample too thin to obey).
  3. GREEN-DAY CONFIRM: BUYs need stock > prev close (w1: green +0.377R vs red
     −0.310R knife-catch). NEUTRAL-tape SELLs need green (fade-the-pop +0.425R);
     BEAR-tape SELLs exempt (aligned momentum shorts). Prev close unknown →
     strict skip (same discipline as M9's offline rule).
  4. NO time-of-day gate (quality setups +R in both windows).
  5. Shared blocks stay: 90/290 previews + pre-09:26 chart window + 1-open.

Forked from M7 (raw control): same F&O universe, engine call, sizing (₹50k / ₹900),
EXITS = FULL M1 SPEC → clean A/B vs M1: any P&L delta comes ONLY from the card.
Back-cast week-1: whitelisted+green card ~23 entries/day at +0.25R gross vs +0.005R
baseline; adding day-proxy side alignment lifted mean R +0.38→+0.44 at ~same net
(in-sample — the forward week is the test; thresholds 55/45 are starting values,
one-line tunables). Every entry/skip logs advances% + regime + daypct so Friday
07-Aug's Coach mines the TRUE breadth×edge relation and the adopt/drop vote reads
per-variant × per-side × per-regime tags.

M1/M2/M5/M6/M7/M8/M9 stay 100% UNTOUCHED (controls intact). Paper-only LAB.

ALERTS: entries + exit legs LOUD, tag 🅼10; silent heartbeat (with regime+adv%) +
loud EOD xlsx. Ledger state10.json · workflow "10. LIVE M10". Joins the Learning
Log automatically as model M10 (label_learn globs learn/raw_M10_*.csv).
Usage: python -u m10_runner.py [--loop N]
"""
import argparse
import json
import time

import pandas as pd

import live_runner as L                  # engine, universe, engine_frame
import learn_log
import feeds, trader, report
import telegram_bot as tg

STATE10 = L.ROOT / "state10.json"

# Coach-v2 best-setup whitelists (scan_code -> canonical name)
BUY_WHITELIST = {102: "BUY-EX17", 104: "BUY-EX5", 105: "BUY-EX6",
                 106: "BUY-EX7", 111: "BUY-EX12"}
SELL_WHITELIST = {208: "SELL-EX8", 212: "SELL-EX12", 213: "SELL-EX12",
                  219: "SELL-EX19", 220: "SELL-EX19"}

# v2.1 breadth thresholds (practical/lenient — one-line tunables after forward data)
BULL_AT = 55.0                      # advances% >= -> BUY side only
BEAR_AT = 45.0                      # advances% <= -> SELL side only
FLIP_CYCLES = 2                     # consecutive cycles past the line to flip regime

# v2.2 (user directive "implement only proven rules"): SECTOR BREADTH RIDES AS
# TELEMETRY ONLY — logged on every entry/skip, ZERO veto power until the forward
# data proves it (w1 evidence was +1.2R/n=7, +1.1R/n=1 — direction agrees, n too
# thin to trade on; Friday's Coach decides with real samples).
def _load_secmap():
    try:
        m = pd.read_csv(L.ROOT / "fno_sector_map.csv")
        d = dict(zip(m["symbol"], m["sector"]))
    except Exception as e:
        print(f"sector map: {type(e).__name__}: {e}")
        d = {}
    by = {}
    for s, sec in d.items():
        by.setdefault(sec, []).append(s)
    return d, by


SEC_OF, SEC_MEMBERS = _load_secmap()


def sector_adv(st, bars_map, today, sym):
    """Sector advance ratio for telemetry: % of the stock's sector members (inside
    the fed universe) with LTP > prev close. Returns (pct, sector, fed_members);
    pct=None when the read is too thin (<3 fed members) or unknown -> permissive."""
    sec = SEC_OF.get(sym)
    if not sec:
        return None, sec, 0
    adv = dec = fed = 0
    for m in SEC_MEMBERS.get(sec, []):
        b = bars_map.get(m)
        if b is None or len(b) == 0:
            continue
        pc = _prev_close(st, m, today)
        if pc is None:
            continue
        fed += 1
        try:
            ltp = float(b["close"].iloc[-1])
        except Exception:
            continue
        if ltp > pc:
            adv += 1
        elif ltp < pc:
            dec += 1
    tot = adv + dec
    pct = round(100.0 * adv / tot, 1) if (fed >= 3 and tot) else None
    return pct, sec, fed


def coach_v2_why(side, code):
    """Stage 1 — best-setup whitelist. None = setup allowed, else skip reason."""
    if side == "BUY" and int(code) not in BUY_WHITELIST:
        return ("not a whitelisted BUY setup — Coach-v2 (w1: plain BUY-EX/EX9/EX4 churn −R)")
    if side == "SELL" and int(code) not in SELL_WHITELIST:
        return ("not a whitelisted SELL setup — Coach-v2 (w1: EX1/EX2/EX3/EX15/NORMAL −R)")
    return None


def coach_v3_side(side, regime):
    """Stage 2 — v2.1 breadth side-gate (only bites on directional tapes)."""
    if regime == "BULL" and side == "SELL":
        return f"BULL tape (adv ≥{BULL_AT:.0f}%) — SELL side blocked (v2.1 alignment)"
    if regime == "BEAR" and side == "BUY":
        return f"BEAR tape (adv ≤{BEAR_AT:.0f}%) — BUY side blocked (v2.1 alignment)"
    return None


def coach_v3_confirm(side, entry, pc, regime):
    """Stage 3 — momentum/fade confirm. None = confirm passed.
    BUY: always needs green (above prev close). SELL: NEUTRAL tape needs green
    (fade); BEAR tape exempt (momentum shorts allowed)."""
    if pc is None:
        return "prev close unavailable — confirm impossible (strict: skip)"
    green = entry > pc
    if side == "SELL" and regime == "BEAR":
        return None
    if not green:
        return (f"below prev close ₹{pc:,.2f} — not green "
                f"(v2.1: red BUY −0.31R · chop-tape red SELL cost-flat)")
    return None


def regime_update(st, pct):
    """Advance/decline regime with 2-cycle anti-flap hysteresis. pct=None -> NEUTRAL."""
    prev = st.get("regime", "NEUTRAL")
    raw = st.get("regime_raw")
    cnt = int(st.get("regime_cnt", 0))
    if pct is None:
        target = "NEUTRAL"
    elif pct >= BULL_AT:
        target = "BULL"
    elif pct <= BEAR_AT:
        target = "BEAR"
    else:
        target = "NEUTRAL"
    if target == prev:
        st["regime_raw"] = None; st["regime_cnt"] = 0
        return prev
    cnt = cnt + 1 if target == raw else 1
    st["regime_raw"] = target; st["regime_cnt"] = cnt
    if cnt >= FLIP_CYCLES:
        st["regime"] = target; st["regime_raw"] = None; st["regime_cnt"] = 0
        return target
    return prev


def load_state(today):
    if STATE10.exists():
        st = json.loads(STATE10.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "pcmap": {}, "regime": "NEUTRAL", "regime_raw": None, "regime_cnt": 0,
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE10.write_text(json.dumps(st, indent=1))


def _prev_close(st, sym, today):
    """Yesterday's close from the 60d history, memoized per day in the state."""
    pm = st.setdefault("pcmap", {})
    if sym not in pm:
        try:
            h = pd.read_csv(L.HIST / f"{sym}.csv", parse_dates=["dt"])
            prev = h[h["dt"].dt.strftime("%Y-%m-%d") < today]
            pm[sym] = round(float(prev["close"].iloc[-1]), 2) if len(prev) else None
        except Exception as e:
            print(f"  prev-close {sym}: {type(e).__name__}")
            pm[sym] = None
    return pm.get(sym)


def breadth_now(st, bars_map, today):
    """Live advance ratio over the fed universe: % of syms with LTP > prev close."""
    adv = dec = 0
    for sym, b in bars_map.items():
        if b is None or len(b) == 0:
            continue
        pc = _prev_close(st, sym, today)
        if pc is None:
            continue
        try:
            ltp = float(b["close"].iloc[-1])
        except Exception:
            continue
        if ltp > pc:
            adv += 1
        elif ltp < pc:
            dec += 1
    tot = adv + dec
    pct = round(100.0 * adv / tot, 1) if tot else None
    return pct, adv, dec, tot


def fmt_m10_alert(tag, tr, key):
    """Loud compact alert lines for entries and exit legs."""
    base = f"{tr['symbol']} {tr['side']} {tr['signal']}"
    if key == "ENTRY":
        return (f"🚨 🅼10 ENTRY · {base}\n"
                f"Time {tr['time']} · ₹{tr['entry']} · Qty {tr['qty']} (₹{tr['capital']:,.0f})\n"
                f"SL ₹{tr['sl']} ({tr.get('sl_anchor', tr.get('sl_mode','structure'))}"
                f" · max loss ₹{tr['risk_rs']:,.0f})\n"
                f"🧠 Coach-v2.1 whitelist · stock {tr.get('daypct','?')}% vs pc ₹{tr.get('pc','?')}"
                f" · tape {tr.get('regime','?')} adv {tr.get('adv','?')}%"
                f" · sec {tr.get('sec','?')} {tr.get('sec_adv','?')}% · exits = M1 spec")
    done = tr.get("closed") or tr.get("status") == "CLOSED"
    tail = (f" · net ₹{tr.get('pnl', 0):+,.0f} ({tr.get('exit_text','')})" if done
            else " · open MTM")
    return f"📤 🅼10 {key} · {base} @ ₹{tr['legs'][-1][2] if tr.get('legs') else '—'}{tail}"


def mode_live():
    """One M10 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M10: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M10: pre-market — idle.")
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

    # --- v2.1 live breadth regime (from the same bars; pc memoized after first pull)
    pct, adv, dec, tot = breadth_now(st, bars_map, today)
    regime = regime_update(st, pct)
    st.setdefault("regime", regime)
    st["gate"] = {"status": f"COACH-V2.1 · tape {regime} (adv {pct}% · {adv}↑/{dec}↓ of {tot})",
                  "source": ("BOTH sides · whitelist BUY{EX17,5,6,7,12}+SELL{EX8,12,19} · "
                             f"side-gate BULL≥{BULL_AT:.0f}%→BUY-only / BEAR≤{BEAR_AT:.0f}%→SELL-only "
                             "(2-cycle anti-flap; NEUTRAL = both) · green-day confirm "
                             "(BEAR-tape SELLs exempt) · sector breadth LOGGED only "
                             "(veto parked: w1 n=7) · no time gate · 90/290 + <09:26 "
                             "blocked · exits = M1 spec (paper lab)")}

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
                    print(f"  >>> M10 {ev['key']} {sym} @ {ev.get('price')}")
                    st["alerts"].append(key)
                    save_state(st)          # registry + state saved BEFORE send (no-dup hardening 03-Aug): crash/resume can never re-send
                    tg.send_message(fmt_m10_alert("🅼10", new_tr, ev["key"]))
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> master signals -> COACH-V2.1 card -> paper entry (causal, per bar close)
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    skipped_now = 0
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
                # POINTER-FIRST (29-Jul cursor fix, user pick A): advance BEFORE the
                # skip/entry dispatch — a `continue` below must never leave this bar
                # un-registered (duplicate skip rows + late gate-flip entries).
                st["signals"][sym] = st["signals"].get(sym, {})
                st["signals"][sym]["nbars"] = j + 1
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
                    pc = None
                    why = None
                    spct = sec = None
                    if int(code) in (90, 290):
                        why = "scanner-table preview (90/290) — no chart label"
                    elif etime < L.CHART_MIN_TIME:
                        why = f"before {L.CHART_MIN_TIME} chart window"
                    elif L.sym_has_open(st, sym):
                        why = "open position already on stock (1-open-trade rule)"
                    else:
                        why = coach_v3_side(side, regime)                   # stage 1: side-gate
                        if why is None:
                            why = coach_v2_why(side, int(code))             # stage 2: whitelist
                        if why is None:
                            spct, sec, _nsm = sector_adv(st, bars_map, today, sym)  # v2.2 telemetry
                            pc = _prev_close(st, sym, today)
                            why = coach_v3_confirm(side, entry, pc, regime) # stage 3: confirm
                    if why:
                        print(f"  M10 {sym} {side} {name} @ {etime} — SKIPPED: {why}")
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": name, "time": etime,
                             "entry": round(entry, 2), "regime": regime, "adv": pct,
                             "sec": sec, "sec_adv": spct, "why": why})
                        skipped_now += 1
                        continue
                    tr = trader.evaluate(sym, side, etime, entry, name, tbars,
                                         warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today))
                    if "error" in tr:
                        print(f"  M10 {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                        continue
                    tr["coach_via"] = f"{side.lower()}-{int(code)}"
                    tr["setup"] = "M10-CoachV2.1"
                    tr["pc"] = pc
                    tr["daypct"] = round((entry - pc) / pc * 100.0, 2) if pc else None
                    tr["regime"] = regime
                    tr["adv"] = pct
                    tr["sec"] = sec                       # v2.2 telemetry (no veto)
                    tr["sec_adv"] = spct
                    tkey, k = sym, 2
                    while tkey in st["trades"]:
                        tkey = f"{sym}#{k}"; k += 1
                    st["trades"][tkey] = tr
                    st["alerts"].append(f"{tkey}:ENTRY")
                    save_state(st)          # registry + state saved BEFORE send (no-dup hardening 03-Aug): crash/resume can never re-send
                    tg.send_message(fmt_m10_alert("🅼10", tr, "ENTRY"))
                    entries_now += 1
                    print(f"  >>> M10 ENTRY {tkey} {side} @ {entry} qty {tr['qty']} · "
                          f"SL {tr['sl']} (v2.1 · tape {regime} adv {pct}% · daypct {tr['daypct']}%)")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M10: Coach-v2.1 entry lab)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append(
                    [it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M10_{today}.xlsx"),
                               skipped=sk or None)
            learn_log.harvest("M10", today, st, None, bars_map)
            msg = ("🅼10 EOD · " + report.summary_text(done, dlbl, st["gate"])
                   + "\n(Coach-v2.1 breadth-aligned lab — A/B vs M1, Friday adopt/drop vote)")
            st["eod_done"] = True
            save_state(st)          # EOD done + state saved BEFORE send (no-dup hardening 03-Aug): report can never double-send
            tg.send_message(msg)
            tg.send_document(out, caption=f"🅼10 📄 M10 Coach-v2.1 paper report {today}")
        except Exception as e:
            print(f"  M10 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status
    if "09:20" <= hhmm < "15:26" and hhmm.endswith(":15"):   # hourly only (alert-noise rule, 03-Aug)
        tg.send_message(f"💓 🅼10 {hhmm} IST · {len(st['trades'])} trades · tape {regime} "
                        f"(adv {pct}%) · whitelist·confirm", silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M10 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · "
          f"entries+{entries_now} · skips {skipped_now} · tape {regime} adv {pct}%")
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
            print(f"--- M10 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
