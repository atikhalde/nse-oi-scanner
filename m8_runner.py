#!/usr/bin/env python3
"""MODEL 8 — M6 + CHASE GUARD paper trader (28-Jul-2026 user pick: option A, standalone model).

EXACT clone of M6 (sector-flow gate + 12-bar timed stable-flow hold + beats-own-sector
leader test) with ONE additional gate — evidence from the 4-session autopsy
(REPORT_M6_LATE_CHASE.md, 20 real M6 trades 22/23/24/28-Jul):

  CHASE GUARD: BUY blocked once the home sector has ALREADY moved > +2.5% today
               (board day_ret metric), SELL blocked once past -2.5%.
               Enter EARLY in the sector move, not after it.

  Measured (filter-only on the 4 days, exits unchanged):
    no guard        20 trades  NET +Rs1,801  (28-Jul: 6 IT gap-chases -Rs573)
    guard 2.0-2.5   12-14 tr   NET +Rs2,021 to +Rs2,752  (boundary: ETERNAL +731 sits
    exactly on the +2.0 line; SWIGGY -380 at -2.38; threshold 2.5 keeps the boundary
    winner, re-admits the crash-chase - user philosophy: do not be too strict, winners big)
  Leader-rank / first-in-sector / count-cap variants all measured WORSE (see report).
  top-30 gainers overlay also measured WORSE (-Rs263 vs guard alone) - rejected twice.

Everything else identical to M6: same causal engine eval, same exits/sizing
(structure SL -/+0.02%, no targets, +1R trail, Rs50k/Rs900, 15:20 sq-off, 1-open/stock),
same kept blocks (90/290 previews, pre-09:26 window). M1/M2/M5/M6/M7 untouched.
Alerts prefix M8. Separate ledger state8.json + workflow "8. LIVE M8".
Usage: python -u m8_runner.py [--loop N]
"""

import argparse
import datetime as dt
import json
import sys
import time

import numpy as np
import pandas as pd

import live_runner as L                  # engine, universe, engine_frame
import learn_log
import feeds, trader, report, flow_map as FM
import telegram_bot as tg

STATE8 = L.ROOT / "state8.json"
SECTOR_OF = dict(pd.read_csv(L.ROOT / "fno_sector_map.csv").values)
OFFICIAL = set(FM.OFFICIAL_TICKERS)
MEMBERS = {}
for sym, sec in SECTOR_OF.items():
    if sec not in OFFICIAL:
        MEMBERS.setdefault(sec, []).append(sym)

# 25-Jul-2026 user pick (option A): timed stable-flow hold 12×5m + beats-own-sector leader filter.
# Hold: a sector that fully qualified stays gate-open for 60 min through one dip unless its
# side flips (mirrors the Pine v4.0 stableFlow intent; the literal single-leader port was
# measured inert in our 30-sector universe — synthetics hog the box).
# Lead: stock day% vs prev close must beat its home sector's day% on the signal side
# (0 margin). Offline 3-day replay of this combo: 11 trades NET +₹982±4
# (22-Jul 1tr −336 · 23-Jul 8tr +1,289 · 24-Jul 2tr +29), 6/6 big winners kept, 54% win.
FM.set_hold("timed", 12)


def load_state(today):
    if STATE8.exists():
        st = json.loads(STATE8.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "eod_done": False, "cycles": 0}


def save_state(st):
    STATE8.write_text(json.dumps(st, indent=1))


def _closed(df, now):
    """closed-bar guard: keep bars whose 5-min close has passed."""
    close_ts = df["dt"] + pd.Timedelta(minutes=5)
    return df[close_ts <= pd.Timestamp(now)]


def _idx_slice(df, today, now):
    df = df.copy()
    df["dt"] = pd.to_datetime(df["dt"], utc=True).dt.tz_convert("Asia/Kolkata") if df["dt"].dt.tz is None else df["dt"]
    is_today = df["dt"].dt.strftime("%Y-%m-%d") == today
    hist = df[~is_today]
    t = _closed(df[is_today], now)
    if t.empty:
        return None
    orb = t.head(6)
    return dict(o=t["open"].values, h=t["high"].values, l=t["low"].values, c=t["close"].values,
                c_full=pd.concat([hist, t])["close"].values,
                prev_close=float(hist["close"].iloc[-1]) if len(hist) else float(t["open"].iloc[0]),
                day_open=float(t["open"].iloc[0]), or_high=float(orb["high"].max()), or_low=float(orb["low"].min()))


def _hist_day(sym, today):
    """yesterday's 5-min bars + the close before them, from committed history."""
    fp = L.HIST / f"{sym}.csv"
    if not fp.exists():
        return None, None
    try:
        h = pd.read_csv(fp, parse_dates=["dt"])
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata") if h["dt"].dt.tz is None else h["dt"]
        d = h["dt"].dt.strftime("%Y-%m-%d")
        ydays = sorted(d[d != today].unique())
        if not ydays:
            return None, None
        yd = h[d == ydays[-1]]
        pd_ = h[d < ydays[-1]].tail(1)
        if yd.empty or pd_.empty:
            return None, None
        return yd.reset_index(drop=True), float(pd_["close"].iloc[-1])
    except Exception:
        return None, None


def _prev_close(sym, today, ycache):
    """previous session's last close for the leader test."""
    if sym not in ycache:
        ycache[sym] = _hist_day(sym, today)
    yd, _ = ycache[sym]
    return float(yd["close"].iloc[-1]) if yd is not None else None


def lead_ok(board, sec, side, sd):
    """beats-own-sector leader test (0 margin): BUY needs stock day% >= sector day%,
    SELL needs stock day% <= sector day%. Waived only if sector metrics missing."""
    d = board["sectors"].get(sec) if sec else None
    if d is None:
        return True, "no sector metrics — lead waived"
    secd = d["day_ret"]
    if side == "BUY" and sd < secd:
        return False, f"lags own sector (stock {sd:+.2f}% vs {sec} {secd:+.2f}%)"
    if side == "SELL" and sd > secd:
        return False, f"lags own sector (stock {sd:+.2f}% vs {sec} {secd:+.2f}%)"
    return True, f"LEAD {sd:+.2f}% vs {sec} {secd:+.2f}%"


# 28-Jul-2026: anti-chase guard (user pick A; REPORT_M6_LATE_CHASE.md). Flip to 2.0 to also
# cut SWIGGY-type crash-chases (-2.38% sector move) at the cost of ETERNAL-type boundary
# winners (+2.00%); measured NET both ways >= +Rs2,020 vs +Rs1,801 unguarded.
CHASE_MAX_SECTOR_MOVE = 2.5


def chase_ok(board, sec, side):
    """enter only EARLY in the sector move: BUY blocked once home sector already ran
    > +2.5% today, SELL blocked once past -2.5% (board day_ret metric — official index for
    official sectors, equal-weight for synthetics). Waived only if sector metrics missing
    (mirrors lead_ok's waiver)."""
    d = board["sectors"].get(sec) if sec else None
    if d is None:
        return True, "no sector metrics — chase-guard waived"
    secd = d["day_ret"]
    if side == "BUY" and secd > CHASE_MAX_SECTOR_MOVE:
        return False, f"chase-guard: {sec} already moved {secd:+.2f}% (>+{CHASE_MAX_SECTOR_MOVE:.1f}%)"
    if side == "SELL" and secd < -CHASE_MAX_SECTOR_MOVE:
        return False, f"chase-guard: {sec} already moved {secd:+.2f}% (<-{CHASE_MAX_SECTOR_MOVE:.1f}%)"
    return True, f"early-in-move ({sec} {secd:+.2f}%)"


def _synth_input(sec, today, now, bars_map, ycache):
    """stitched yesterday+today equal-weight level series for a synthetic sector."""
    tp, yp = {"o": [], "h": [], "l": [], "c": []}, {"o": [], "h": [], "l": [], "c": []}
    grid_t = grid_y = None
    for sym in MEMBERS.get(sec, []):
        b = bars_map.get(sym)
        if b is None:
            continue
        if sym not in ycache:
            ycache[sym] = _hist_day(sym, today)
        yd, pcy = ycache[sym]
        if yd is None:
            continue
        tt = _closed(b, now)
        if tt.empty:
            continue
        pct = float(yd["close"].iloc[-1])
        tt = tt.set_index("t"); yy = yd.assign(t=yd["dt"].dt.strftime("%H:%M")).set_index("t")
        if grid_t is None:
            grid_t = tt.index.tolist()
        if grid_y is None:
            grid_y = yy.index.tolist()
        for col, k in (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c")):
            tp[k].append((tt[col] / pct - 1).reindex(grid_t).values)
            yp[k].append((yy[col] / pcy - 1).reindex(grid_y).values)
    if not tp["c"]:
        return None
    def lvl(parts, z):
        return z * (1 + np.nanmean(np.array(parts), axis=0))
    yl = {k: lvl(yp[k], 100.0) for k in "ohlc"}
    base = float(yl["c"][-1])
    tl = {k: lvl(tp[k], base) for k in "ohlc"}
    return dict(o=tl["o"], h=tl["h"], l=tl["l"], c=tl["c"],
                c_full=np.concatenate([yl["c"], tl["c"]]),
                prev_close=base, day_open=float(tl["o"][0]),
                or_high=float(np.nanmax(tl["h"][:6])), or_low=float(np.nanmin(tl["l"][:6])))


def build_board(now, today, bars_map, ycache):
    """Full sector board at cycle time. Returns (board, meta) or (None, reason)."""
    secs = {}
    fetch_ok = fetch_fail = 0
    for sec, ticker in FM.OFFICIAL_TICKERS.items():
        df = FM.fetch_index_bars(ticker, "5d")
        if df is None:
            fetch_fail += 1
            continue
        s = _idx_slice(df, today, now)
        if s is not None:
            secs[sec] = s
            fetch_ok += 1
        time.sleep(0.25)
    for sec in MEMBERS:
        s = _synth_input(sec, today, now, bars_map, ycache)
        if s is not None:
            secs[sec] = s
    nif_df = FM.fetch_index_bars(FM.NIFTY_TICKER, "2d")
    if nif_df is None or not secs:
        return None, f"index feeds down (nifty {'ok' if nif_df is not None else 'DOWN'}, sectors {fetch_ok}/17)"
    nif = _idx_slice(nif_df, today, now)
    if nif is None:
        return None, "nifty slice empty"
    n_intra = (nif["c"] - nif["day_open"]) / nif["day_open"] * 100.0
    vix_df = FM.fetch_index_bars(FM.VIX_TICKER, "1d")
    vix = None
    if vix_df is not None:
        v = _idx_slice(vix_df, today, now)
        vix = float(v["c"][-1]) if v is not None else None
    mins = (now.hour - 9) * 60 + now.minute - 15
    board = FM.compute_board(secs, {"n_intra": n_intra}, vix, mins)
    return board, f"official {fetch_ok}/17 ok · synth {sum(1 for s in MEMBERS if s in secs)}/13"


def mode_live():
    """One M8 cycle. Returns True if a cycle ran, False if idle."""
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("M8: EOD done — idle.")
        save_state(st); return False
    if hhmm < "09:16":
        print("M8: pre-market — idle.")
        save_state(st); return False

    # --- fetch today's stock bars (needed for scanner AND synthetic sector baskets)
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

    # --- sector board (strict: must be computable — else no new entries this cycle)
    ycache = {}
    board, meta = build_board(now, today, bars_map, ycache)
    print(f"M8 board: {meta}")
    gate_ok = board is not None
    st["gate"] = {"status": "OK" if gate_ok else "OFFLINE", "source": meta}
    if gate_ok:
        st["sector_board"] = {"time": hhmm, "net_tilt": board["net_tilt"], "tilt": board["tilt_label"],
                              "bull_q": board["bull_q"][:4], "bear_q": board["bear_q"][:4],
                              "persist_bull": board["persist_bull"], "persist_bear": board["persist_bear"],
                              "hold": board.get("hold_mode"), "held_bull": board.get("held_bull"),
                              "held_bear": board.get("held_bear")}

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
            new_tr["sector"] = tr.get("sector")
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    tg.send_message("🅼8 · " + trader.fmt_alert(new_tr, ev["key"]))
                    st["alerts"].append(key)
                    save_state(st)          # persist alert registry instantly (no-repeat guarantee)
        except Exception as e:
            print(f"  manage {tkey}: {type(e).__name__}: {e}")

    # --- engine -> master signals -> M8 sector gate -> paper entry (causal, once per bar close)
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    entries_now = 0
    skipped_sector = 0
    for sym, tbars in bars_map.items():
        tbars_c = _closed(tbars, now)
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
                    sec = SECTOR_OF.get(sym)
                    why = None
                    if int(code) in (90, 290):
                        why = "scanner-table preview (90/290) — no chart label"
                    elif etime < L.CHART_MIN_TIME:
                        why = f"before {L.CHART_MIN_TIME} chart window"
                    elif not gate_ok:
                        why = "sector board offline (strict)"
                    elif L.sym_has_open(st, sym):
                        why = "open position already on stock (1-open-trade rule)"
                    else:
                        ok, why2 = board["entry_ok"](side, sec)
                        if ok:
                            cok, ctxt = chase_ok(board, sec, side)
                            if cok:
                                why2 = f"{why2} · {ctxt}"
                            else:
                                ok, why2 = False, ctxt
                        if ok:
                            pc = _prev_close(sym, today, ycache)
                            if pc:
                                lok, ltxt = lead_ok(board, sec, side, (entry / pc - 1) * 100)
                                if lok:
                                    why2 = f"{why2} · {ltxt}"
                                else:
                                    ok, why2 = False, ltxt
                        if not ok:
                            why = why2; skipped_sector += 1
                    if why:
                        print(f"  M8 {sym} {side} {name} @ {etime} — SKIPPED: {why}")
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": name, "time": etime,
                             "entry": round(entry, 2), "sector": sec, "why": why})
                        continue
                    tr = trader.evaluate(sym, side, etime, entry, name, tbars,
                                         warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today))
                    if "error" in tr:
                        print(f"  M8 {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                        continue
                    tr["sector"] = sec
                    tkey, k = sym, 2
                    while tkey in st["trades"]:
                        tkey = f"{sym}#{k}"; k += 1
                    st["trades"][tkey] = tr
                    st["alerts"].append(f"{tkey}:ENTRY")
                    save_state(st)          # persist alert registry instantly (no-repeat guarantee)
                    suffix = f" · #{k-1} on {sym}" if tkey != sym else ""
                    tg.send_message("🅼8 · " + trader.fmt_alert(tr, "ENTRY")
                                    + f"\n🏭 sector <b>{sec}</b> — {why2}{suffix}")
                    entries_now += 1
                    print(f"  >>> M8 ENTRY {tkey} {side} @ {entry} [{sec}] ({why2})")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y") + " (M8: M8 + chase guard)"
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append(
                    [it["symbol"], it["side"], it["signal"] + f" [{it.get('sector') or '-'}]", it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(L.ROOT / f"paper_test_M8_{today}.xlsx"), skipped=sk or None)
            learn_log.harvest("M8", today, st, None, bars_map, board=board if gate_ok else None)
            msg = "🅼8 EOD · " + report.summary_text(done, dlbl, st["gate"])
            if gate_ok:
                msg += "\n\n" + FM.board_text(board)
            tg.send_message(msg)
            tg.send_document(out, caption=f"🅼8 📄 M8 sector-flow paper report {today}")
            st["eod_done"] = True
            save_state(st)          # so a crashed run never re-sends the EOD report
        except Exception as e:
            print(f"  M8 EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status
    if "09:20" <= hhmm < "15:26":
        lead = "—"
        if gate_ok:
            b1 = " · ".join(f"{n} {s:.0f}" for n, s in board["bull_q"][:2]) or "—"
            s1 = " · ".join(f"{n} {s:.0f}" for n, s in board["bear_q"][:2]) or "—"
            lead = f"🐂{b1} · 🐻{s1} · {board['tilt_label']}"
        tg.send_message(f"💓 🅼8 {hhmm} IST · {len(st['trades'])} trades · board {'OK' if gate_ok else 'OFFLINE'} · {lead}", silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"M8 cycle done: {len(st['trades'])} trades · {len(bars_map)} fed · entries+{entries_now} · "
          f"sector-skips {skipped_sector} · board {'OK' if gate_ok else 'OFFLINE'}")
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
            print(f"--- M8 loop: cycle {i + 2} of {a.loop} in ~240s ---")
            time.sleep(240)
