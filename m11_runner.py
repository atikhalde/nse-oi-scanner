#!/usr/bin/env python3
"""MODEL 11 — MASTER × VIDEO-4 ALIGNMENT LAB (02-Aug-2026, user vote B: pure
alignment; green-day/red-day confirm deferred to a later test).

The 4 entry setups from Pro Trader Aakash's "Secret Entry Setup I Use Daily"
(YouTube CXE-RrP5_ys, transcribed + rule-extracted in-session) wired as the
alignment test for MASTER SCANNER signals:

  Entry = (ANY real master signal, BOTH sides — no variant whitelist)
        + time >= 09:45 (user call 02-Aug: the first-30-min window defines the base;
          was 10:00)
        + AT LEAST ONE of the 4 setup detectors TRUE at the signal bar.

  S1 MORNING BASE     BUY: day's low == first-30-min low (09:15–09:45), nothing
                      after 09:45 made a lower low. SELL: day high == first-30-min
                      high. (Video: base intact => morning buyers/sellers in control.)
  S2 PIVOT PULLBACK   classic daily pivot P = yesterday (H+L+C)/3 from history.
                      EXACT order only: some earlier close beyond P -> wick
                      touches P within the last 6 bars (may dip toward the deeper
                      level, per video) -> signal bar closes back beyond P.
  S3 FLAG BREAKOUT    pole: move >= POLE_MIN_PCT (0.6%) in <=6 consecutive bars
                      within the last ~24; flag: 2–8 bars after the pole with total
                      range <= FLAG_RATIO (0.4) x pole points (box or triangle);
                      signal bar closes beyond the flag (BUY: above flag high).
  S4 SANDWICH         trend-day proxy: S1 side intact OR close[j-3] beyond
                      close[j-13]. BUY: green-red-green with the red trapped inside
                      the two greens' envelope; signal bar closes above the
                      envelope high. SELL mirrors (red-green-red).

USER RULES 02-Aug (asked + confirmed 1a/2b/3b):
  1a S1 MORNING BASE IS NOT A TRIGGER ALONE — entry needs >=1 core setup (S2/S3/S4)
     TRUE; S1 counts only as extra confluence. Blocked S1-alone signals are logged
     in the EOD skip sheet for measurement.
  2b DAY-COLOR MOMENTUM CONFIRM (strict): BUY only on GREEN day (entry > prev close),
     SELL only on RED day (entry < prev close). Flat or unknown prev close => BLOCKED
     for both sides (static rule, never loosens intra-day).
  3b NO-DUP ALERT HARDENING + QUIET: alert keys are registered + state saved BEFORE
     the Telegram send (a crashed/resumed run can never send the same alert twice);
     heartbeat throttled to 1x/hour. ENTRY / TRAIL_ON / EXIT alerts kept.

Each trade/skipped signal is tagged with the EXACT detectors that matched
(e.g. tr["setups"]=["S1","S2"]) so Friday's Coach votes keep/drop PER SETUP —
the setups earn their place one by one, same bar as every rule (paper-only LAB;
M1/M2/M5/M6/M7/M8/M9/M10 untouched; controls intact).

Forked from M10 (itself forked from M7 raw control): same F&O universe, engine
call, sizing (₹50k / ₹900). EXITS = EXACT M8 SPEC (user request, 02-Aug): both
runners call the same trader.evaluate with the same defaults — setup-aware
structure SL ∓0.02%, NO fixed targets, +1R structure-swing/EMA9/chandelier
trail, max-loss ₹900 qty cap, 15:20 square-off → clean A/B vs M1/M8 controls.

ALERTS: M8-format rich alerts (trader.fmt_alert core, 🅼11 prefix) + M11 extras:
  ENTRY shows setup NAME (price-action class), SL with anchor + max loss,
  🎯 TARGET line = the +1R trail-arm level (M8 spec has no fixed targets — the
  +1R level is the working "target": trail arms there, runner rides to 15:20),
  and the 🎬 video setup names (S1 Morning Base / S2 Pivot Pullback /
  S3 Flag Breakout / S4 Sandwich). TP1/TP2/TRAIL_ON/EXIT_SL/EXIT_EOD legs use
  the exact M8 texts. Silent heartbeat + LOUD EOD paper report (summary + xlsx)
  at market close like every other model. Ledger state11.json · workflow
  "11. LIVE M11". Joins the Learning Log automatically as model M11
  (learn cls column carries the video-setup tag, e.g. "S1+S2").

M11-ONLY TELEGRAM FANOUT (user request, 02-Aug): every M11 alert goes to the
usual main bot chat AND, once the 4 extra secrets exist, to 2 SEPARATE bots —
M11_BOT_TOKEN_A / M11_CHAT_ID_A and M11_BOT_TOKEN_B / M11_CHAT_ID_B. Routing is
conditional: if the extra secrets are not set the runner falls back to the main
chat only, so nothing ever goes silent. Flip M11_INCLUDE_MAIN to False to make
M11 reach ONLY the 2 new bots (main chat stays M1/M2/M5–M10). All other models
keep using telegram_bot's default single-chat path — untouched.
Usage: python -u m11_runner.py [--loop N]
"""
import argparse
import json
import os
import time

import pandas as pd

import live_runner as L                  # engine, universe, engine_frame
import learn_log
import feeds, trader, report
import telegram_bot as tg

STATE11 = L.ROOT / "state11.json"

# Video-4 tunables (seeded from the video + market practice; calibrate after data)
MORNING_BARS_TILL = "09:45"       # first-30-min window end (inclusive)
ENTRY_MIN_M11 = "09:45"           # video/user call 02-Aug: base window done by ~30 min
PULLBACK_WIN = 6                  # S2: touch window (last N bars before signal)
POLE_MIN_PCT = 0.6                # S3: minimum pole move %
POLE_MIN_BARS = 3                 # S3: pole needs >= N consecutive bars
POLE_MAX_LEN = 6                  # S3: pole up to N consecutive bars
POLE_LOOKBACK = 24                # S3: scan this many bars back for pole end
FLAG_RATIO = 0.4                  # S3: flag range <= ratio x pole move (points)
FLAG_MIN_BARS = 2
FLAG_MAX_BARS = 8
TREND_WIN = 10                    # S4 trend proxy lookback (bars)

# M11 telegram routing (02-Aug): 2 separate extra bots in ADDITION to main chat.
M11_INCLUDE_MAIN = True           # True: main chat + 2 extra bots · False: extras only


# ---------------- M11-only telegram fanout (other models untouched) ----------------

def m11_targets():
    """Extra (bot_token, chat_id) pairs from secrets M11_BOT_TOKEN_x / M11_CHAT_ID_x."""
    extra = []
    for suf in ("A", "B"):
        tok = os.environ.get(f"M11_BOT_TOKEN_{suf}", "").strip()
        chat = os.environ.get(f"M11_CHAT_ID_{suf}", "").strip()
        if tok and chat:
            extra.append((tok, chat))
    return extra


def _send_m11(text, silent=False):
    """M11 alert fanout: main chat (per M11_INCLUDE_MAIN) + every extra bot.
    Fallback: if no extra bot is configured the main chat always gets the alert —
    an unconfigured secret must NEVER silence a trade alert."""
    extra = m11_targets()
    if M11_INCLUDE_MAIN or not extra:
        tg.send_message(text, silent=silent)
    for tok, chat in extra:
        r = tg._post(f"{tg.API}/bot{tok}/sendMessage",
                     json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True, "disable_notification": silent})
        if r is None or not getattr(r, "ok", False):
            print(f"M11 extra-bot {chat}: message failed {getattr(r, 'status_code', '—')}")


def _doc_m11(path, caption=""):
    """M11 end-of-day xlsx fanout to the same targets as _send_m11."""
    extra = m11_targets()
    if M11_INCLUDE_MAIN or not extra:
        tg.send_document(path, caption=caption)
    for tok, chat in extra:
        try:
            with open(path, "rb") as f:
                tg._post(f"{tg.API}/bot{tok}/sendDocument",
                         data={"chat_id": chat, "caption": caption, "parse_mode": "HTML"},
                         files={"document": f}, timeout=60)
        except Exception as e:
            print(f"M11 extra-bot {chat}: document failed {type(e).__name__}: {e}")


# ---------------- the 4 video setup detectors (pure functions) ----------------

def s1_morning_base(bars, side):
    """Video S1: for BUY the day's low must be the first-30-min low (no new low
    after 09:45); for SELL the day's high must be the first-30-min high."""
    first = bars[bars["t"] <= MORNING_BARS_TILL]
    if first.empty:
        return False
    if side == "BUY":
        return float(bars["low"].min()) == float(first["low"].min())
    return float(bars["high"].max()) == float(first["high"].max())


def s2_pivot_pullback(bars, j, side, P):
    """Video S2 (fidelity 02-Aug): the ONLY sequence that matters —
      beyond the pivot (any earlier close)  ->  retrace: wick touches/crosses the
      pivot within the last PULLBACK_WIN bars (video allows dipping 'toward the
      second level')  ->  the signal bar closes back BEYOND the pivot.
    The break may be fresh (HCL-Tech example: cross happened right before the
    pullback) — order, not location, is the rule."""
    if P is None or j < PULLBACK_WIN + 2:
        return False
    c = bars["close"].values
    lo = bars["low"].values; hi = bars["high"].values
    w0 = max(0, j - PULLBACK_WIN)
    if side == "BUY":
        touches = [i for i in range(w0, j) if lo[i] <= P]
        if not touches:
            return False
        i_t = touches[-1]                                   # current pullback
        beyond = any(c[i] > P for i in range(i_t))
        return bool(beyond and float(c[j]) > P)
    touches = [i for i in range(w0, j) if hi[i] >= P]
    if not touches:
        return False
    i_t = touches[-1]
    beyond = any(c[i] < P for i in range(i_t))
    return bool(beyond and float(c[j]) < P)


def s3_flag_breakout(bars, j, side):
    """Video S3: pole (>=0.6% impulsive leg) + compressed flag + close-out breakout."""
    if j < 8:
        return False
    o = bars["open"].values; h = bars["high"].values
    lo = bars["low"].values; c = bars["close"].values
    start_min = max(POLE_MAX_LEN - 1, j - POLE_LOOKBACK)
    for end in range(j - FLAG_MIN_BARS, start_min - 1, -1):
        for plen in range(POLE_MIN_BARS, POLE_MAX_LEN + 1):
            s0 = end - plen + 1
            if s0 < 1:
                continue
            move = c[end] - c[s0]
            refs = c[max(0, s0 - 1)]
            if refs <= 0:
                continue
            pole_pct = move / refs * 100.0
            pole_pts = abs(c[end] - c[s0])
            if pole_pts <= 0:
                continue
            flag = slice(end + 1, j)
            nb = j - end - 1
            if not (FLAG_MIN_BARS <= nb <= FLAG_MAX_BARS):
                continue
            frange = float(h[flag].max() - lo[flag].min())
            if side == "BUY":
                if pole_pct >= POLE_MIN_PCT and frange <= FLAG_RATIO * pole_pts:
                    if float(c[j]) > float(h[flag].max()):
                        return True
            else:
                if pole_pct <= -POLE_MIN_PCT and frange <= FLAG_RATIO * pole_pts:
                    if float(c[j]) < float(lo[flag].min()):
                        return True
    return False


def s4_sandwich(bars, j, side):
    """Video S4: trapped counter-candle between two trend candles, then breakout.
    Signal bar IS the breakout bar (video: breakout after the trap, not just paused)."""
    if j < 4:
        return False
    o = bars["open"].values; h = bars["high"].values
    lo = bars["low"].values; c = bars["close"].values
    o3, h3, l3, c3 = float(o[j-3]), float(h[j-3]), float(lo[j-3]), float(c[j-3])
    o2, h2, l2, c2 = float(o[j-2]), float(h[j-2]), float(lo[j-2]), float(c[j-2])
    o1, h1, l1, c1 = float(o[j-1]), float(h[j-1]), float(lo[j-1]), float(c[j-1])
    trend = True
    if j >= 13:
        base = float(c[j - 13])
        trend = (c3 > base) if side == "BUY" else (c3 < base)
    cur = float(c[j])
    if side == "BUY":
        pat = (o3 < c3) and (o2 > c2) and (o1 < c1)                    # green-red-green
        trap = (h2 <= max(h3, h1)) and (l2 >= min(l3, l1))
        brk = cur > max(h3, h1)
    else:
        pat = (o3 > c3) and (o2 < c2) and (o1 > c1)                    # red-green-red
        trap = (l2 >= min(l3, l1)) and (h2 <= max(h3, h1))
        brk = cur < min(l3, l1)
    return bool(trend and pat and trap and brk)


def video_setups(bars, j, side, pv):
    """Which of the 4 setups are TRUE at signal bar j. Returns sorted list like ['S1','S3']."""
    got = []
    if s1_morning_base(bars.iloc[: j + 1], side):
        got.append("S1")
    if s2_pivot_pullback(bars, j, side, pv):
        got.append("S2")
    if s3_flag_breakout(bars, j, side):
        got.append("S3")
    if s4_sandwich(bars, j, side):
        got.append("S4")
    return got


def test_alert():
    """--test-alert mode: fire a synthetic M11 alert through the REAL fanout path
    (_send_m11 → main + extra bots A/B). Prints the target count to the workflow
    log so fanout can be verified even without seeing secret values. No trading
    state touched."""
    tr = {"symbol": "KAYNES", "side": "BUY", "signal": "BUY-EX17", "time": "10:05",
          "entry": 3376.0, "qty": 14, "capital": 47264.0, "sl": 3360.1,
          "sl_anchor": "pullback-low ∓0.02%", "risk_pts": 15.9, "risk_rs": 900.0,
          "cls_trader": "PULLBACK", "setup": "S1+S2", "trail_style": "structure swings",
          "sl_mode": "structure", "setups": ["S1", "S2"], "legs": [], "events": [],
          "pnl": 0.0, "r_total": 0.0, "exit_text": "", "closed": False, "status": "OPEN"}
    extra = m11_targets()
    to_main = M11_INCLUDE_MAIN or not extra
    n = (1 if to_main else 0) + len(extra)
    _send_m11("🧪 🅼11 TEST — hello from the M11 lab. If this reaches the correct "
              f"chat(s), the {n}-target fanout is wired right.")
    _send_m11(fmt_m11_alert(tr, "ENTRY"))
    print(f"M11 test alert dispatched to {n} target(s): "
          f"main={'yes' if to_main else 'no'} extras={len(extra)}")
    return n


# ---------------- plumbing (same discipline as M7/M10) ----------------

def load_state(today):
    if STATE11.exists():
        st = json.loads(STATE11.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "pvmap": {}, "eod_done": False, "cycles": 0}


def save_state(st):
    STATE11.write_text(json.dumps(st, indent=1))


def _pivot(st, sym, today):
    """Classic daily pivot + previous close from yesterday's history bar
    (one read, memoized per day: pivot in pvmap, prev close in pcmap — pcmap feeds
    the 2b day-color momentum confirm)."""
    pm = st.setdefault("pvmap", {})
    cm = st.setdefault("pcmap", {})
    if sym not in pm:
        pm[sym] = cm[sym] = None
        try:
            h = pd.read_csv(L.HIST / f"{sym}.csv", parse_dates=["dt"])
            prev = h[h["dt"].dt.strftime("%Y-%m-%d") < today]
            if len(prev):
                pm[sym] = round((float(prev["high"].iloc[-1]) + float(prev["low"].iloc[-1])
                                 + float(prev["close"].iloc[-1])) / 3.0, 2)
                cm[sym] = float(prev["close"].iloc[-1])
        except Exception as e:
            print(f"  pivot {sym}: {type(e).__name__}")
    return pm.get(sym)


def core_setups(setups):
    """Rule 1a: S1 alone is never a trigger — returns only the core setups (S2/S3/S4).
    Empty result => entry blocked even though a video setup matched."""
    return [t for t in setups if t != "S1"]


def daycolor_ok(side, entry, pc):
    """Rule 2b (user pick B): strict day-color momentum confirm.
    BUY: green day only (entry > prev close). SELL: red day only (entry < prev close).
    Flat day or unknown prev close => False (static, never loosens)."""
    if pc is None:
        return False
    return float(entry) > float(pc) if side == "BUY" else float(entry) < float(pc)


SETUP_NAMES = {"S1": "Morning Base", "S2": "Pivot Pullback",
               "S3": "Flag Breakout", "S4": "Sandwich"}


def fmt_m11_alert(tr, key):
    """M11 alert = EXACT M8 alert core (trader.fmt_alert, 🅼11 prefix) + M11 lines.
    ENTRY adds: 🎯 TARGET = +1R trail-arm level (M8 exit spec has no fixed targets;
    the trail arms at +1R and the runner rides to 15:20) and 🎬 video setup names."""
    msg = "🅼11 · " + trader.fmt_alert(tr, key)
    if key == "ENTRY":
        s = 1 if tr["side"] == "BUY" else -1
        risk = float(tr.get("risk_pts") or abs(float(tr["entry"]) - float(tr["sl"])))
        arm = round(float(tr["entry"]) + s * trader.TRAIL_ARM_R * risk, 2)
        names = " · ".join(f"{t} {SETUP_NAMES.get(t, t)}" for t in tr.get("setups", [])) or "—"
        msg += (f"\n🎯 TARGET ₹{arm} (+1R trail-arm — no fixed targets, M8 exit spec)"
                f"\n🎬 Entry setup: {names}")
    return msg


def mode_live():
    """One M11 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M11: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M11: pre-market — idle.")
        save_state(st); return False

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

    st["gate"] = {"status": "MASTER×VIDEO-4 (pure alignment, vote B)",
                  "source": ("any master signal, BOTH sides · ≥1 of S1 morning-base / "
                             "S2 pivot-pullback / S3 flag-breakout / S4 sandwich TRUE at "
                             "signal bar · entries ≥09:45 · 90/290 + <09:26 blocked · "
                             "rule1a: S1 alone blocked (needs S2/S3/S4) · rule2b: day-color "
                             "confirm (BUY green day / SELL red day, strict) · "
                             "exits = EXACT M8 spec: structure SL ∓0.02% · no fixed targets "
                             "(+1R trail-arm) · ₹900 max-loss · 15:20 sq-off (paper lab)")}

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
            new_tr["setups"] = tr.get("setups", [])        # M8-style: thread custom tags
            new_tr["cls_trader"] = tr.get("cls_trader") or new_tr.get("setup")
            new_tr["setup"] = tr.get("setup", new_tr.get("setup"))   # keep video tag (learn cls)
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    print(f"  >>> M11 {ev['key']} {sym} @ {ev.get('price')}")
                    st["alerts"].append(key)
                    save_state(st)          # registry-first (rule 3b): crash/resume can never re-send
                    _send_m11(fmt_m11_alert(new_tr, ev["key"]))
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> master signals -> VIDEO-4 alignment -> paper entry
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
                # POINTER-FIRST (29-Jul cursor fix, user pick A)
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
                    why = None
                    setups = []
                    daypct = None
                    if int(code) in (90, 290):
                        why = "scanner-table preview (90/290) — no chart label"
                    elif etime < L.CHART_MIN_TIME:
                        why = f"before {L.CHART_MIN_TIME} chart window"
                    elif L.sym_has_open(st, sym):
                        why = "open position already on stock (1-open-trade rule)"
                    elif etime < ENTRY_MIN_M11:
                        why = f"before {ENTRY_MIN_M11} — morning base not verified yet (video pre-condition)"
                    else:
                        pv = _pivot(st, sym, today)
                        pc = st.get("pcmap", {}).get(sym)
                        daypct = round((entry / pc - 1) * 100, 3) if pc else None
                        setups = video_setups(tbars.iloc[: j + 1], j, side, pv)
                        if not setups:
                            why = "no video setup aligned at signal bar (S1/S2/S3/S4 all false)"
                        elif not core_setups(setups):
                            why = "S1 alone blocked — morning base is a bonus tag, needs S2/S3/S4 (rule 1a)"
                        elif not daycolor_ok(side, entry, pc):
                            why = ("day-color confirm blocked: BUY only on GREEN day"
                                   if side == "BUY" else
                                   "day-color confirm blocked: SELL only on RED day") + \
                                  f" (entry {entry:.2f} vs prev close {pc}, rule 2b)"
                    if why:
                        print(f"  M11 {sym} {side} {name} @ {etime} — SKIPPED: {why}")
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": name, "time": etime,
                             "entry": round(entry, 2), "setups": setups, "daypct": daypct,
                             "why": why})
                        skipped_now += 1
                        continue
                    tr = trader.evaluate(sym, side, etime, entry, name, tbars,
                                         warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today))
                    if "error" in tr:
                        print(f"  M11 {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                        continue
                    tr["cls_trader"] = tr.get("setup")              # trader classify (alert Setup line)
                    tr["setups"] = setups
                    tr["setup"] = "+".join(setups)                  # learn 'cls' col: e.g. "S1+S2" (Friday votes)
                    tr["daycolor"] = "GREEN" if daypct and daypct > 0 else "RED"
                    tr["daypct"] = daypct
                    tkey, k = sym, 2
                    while tkey in st["trades"]:
                        tkey = f"{sym}#{k}"; k += 1
                    st["trades"][tkey] = tr
                    st["alerts"].append(f"{tkey}:ENTRY")
                    save_state(st)          # registry-first (rule 3b): crash/resume can never re-send
                    _send_m11(fmt_m11_alert(tr, "ENTRY"))
                    entries_now += 1
                    print(f"  >>> M11 ENTRY {tkey} {side} @ {entry} qty {tr['qty']} · "
                          f"SL {tr['sl']} · setups {setups}")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M11: master × video-4)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append(
                    [it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M11_{today}.xlsx"),
                               skipped=sk or None)
            learn_log.harvest("M11", today, st, None, bars_map)
            sc = {}
            for t in done:
                for s_ in t.get("setups", []):
                    sc[s_] = sc.get(s_, 0) + 1
            sct = " · ".join(f"{k}×{sc[k]}" for k in sorted(sc)) if sc else "—"
            msg = ("🅼11 EOD · " + report.summary_text(done, dlbl, st["gate"])
                   + f"\n🎬 video setups taken: {sct}"
                   + "\n(master × video-4 lab — per-setup adoption votes Friday)")
            st["eod_done"] = True
            save_state(st)          # registry-first (rule 3b): EOD report can never double-send
            _send_m11(msg)
            _doc_m11(out, caption=f"🅼11 📄 M11 video-4 paper report {today}")
        except Exception as e:
            print(f"  M11 EOD report: {type(e).__name__}: {e}")

    if "09:20" <= hhmm < "15:26" and hhmm.endswith(":15"):   # rule 3b: 1x/hour, not every cycle
        _send_m11(f"💓 🅼11 {hhmm} IST · {len(st['trades'])} trades · master×video-4 "
                  f"(hourly check-in)", silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M11 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · "
          f"entries+{entries_now} · skips {skipped_now}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--test-alert", action="store_true",
                    help="send one synthetic M11 alert to all targets and stop")
    a = ap.parse_args()
    if a.test_alert:
        test_alert()
        raise SystemExit(0)
    for i in range(max(1, a.loop)):
        active = mode_live()
        if not active:
            break
        if i < a.loop - 1:
            print(f"--- M11 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
