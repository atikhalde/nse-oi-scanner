#!/usr/bin/env python3
"""Forward paper-test runner — MASTER SECTOR BATCH SCANNER (100% intact engine)
+ TOP-30 strong OI-spurt gate + ₹50k paper sizing + Telegram alerts.

Modes:
  --test                connectivity/self test (secrets, feeds, engine, telegram)
  --bootstrap           download ~90d history for all 210 stocks -> data/history/
                        (+ prev-day combined OI base via NSE fo bhavcopy, best effort)
  --live                one cycle: fetch today's bars, run engine, gate, trade, alert
  --live --loop N       N consecutive cycles (~4 min apart) — cron-robust coverage
  --replay YYYY-MM-DD   offline validation with cached bars + an entry CSV
                        (columns: time,symbol,side,entry[,signal]) — no engine rerun.

State lives in state.json (committed back to the repo by the workflow).
Env: DHAN_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (all optional).
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))  # flat layout: all modules at repo root


def _load_dotenv():
    """VM mode: read KEY=VALUE pairs from .env next to this file (if present).
    No dependency, never overrides real environment variables."""
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

import master_scanner as ms
import feeds, gate, trader, report, telegram_bot as tg

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATE = ROOT / "state.json"
HIST = ROOT / "data" / "history"
MASTER_CODES = set(range(101, 113)) | set(range(201, 221)) | {80, 90, 280, 290}
# B2 quality filter (approved 23-Jul review, 2-day evidence): trade only chart
# signals EX1-EX8 + NORMAL; EX9+ weak variants blocked all day; fresh EX1/EX2
# at the open (razor-edge class, EICHERMOT-type Dhan/TV tick flips, net loser
# group) blocked until EX_OPEN_FROM.
# Buy codes are offset vs names: 101=BUY-EX(base) 102=BUY-EX17 103..112=BUY-EX4..EX13
# Sell codes match names: 201=SELL-EX1 ... 220=SELL-EX19 (206=SELL-EX5S)
EX_WEAK_CODES = {102} | set(range(108, 113)) | set(range(210, 221))  # name>=EX9: buy EX17/EX9-EX13, sell EX9-EX19
EX_RAZOR_CODES = {101, 201, 202}                               # strictest class: BUY-EX base / SELL-EX1 / SELL-EX2
EX_OPEN_FROM = "09:45"
# TradingView chart truth: nothing fires before the (9,26) window, so the first
# tradeable 5-min bar is 09:30. Signals at 09:15/09:20/09:25 are logged + skipped
# (chart-window rule, user-confirmed 23-Jul-2026).
CHART_MIN_TIME = "09:26"

UNI = pd.read_csv(ROOT / "fno_universe.csv")
SYMS = UNI["symbol"].tolist()
SID = dict(zip(UNI["symbol"], UNI["securityId"]))


def now_ist():
    return dt.datetime.now(dt.timezone.utc).astimezone(IST)


def load_state(today):
    if STATE.exists():
        st = json.loads(STATE.read_text())
        if st.get("date") == today:
            return st
    return {"date": today, "signals": {}, "trades": {}, "alerts": [], "gate": {},
            "gate_rank": {}, "eod_done": False, "cycles": 0}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1))


def fmt_raw_signal(sym, side, name, etime, entry, rank, gate_ok):
    """Telegram text for a master signal that did NOT become a paper trade
    (outside the TOP-30 OI-spurt gate). Sent once per signal; info-only."""
    head = (f"📡 <b>MASTER SIGNAL — {sym} {side}</b>\n"
            f"{name} @ {etime} · ₹{entry:,.2f}")
    if not gate_ok:
        why = "⚠️ OI-spurt gate offline — ungated signal (strict mode: no trade)"
    elif rank is None:
        why = "🚫 Not on NSE OI-spurt list today — no trade"
    else:
        why = f"🚫 OI-spurt rank #{rank} (outside TOP-30) — no trade"
    return head + "\n" + why


def sym_has_open(st, sym):
    """1-OPEN-TRADE-PER-STOCK rule (user): True while ANY paper trade on sym is
    still open. Trades are re-evaluated deterministically every cycle, so
    'closed' reflects the newest bars — a SL/target that printed on a bar frees
    the stock for a signal evaluated on that very bar (same-bar flip allowed)."""
    for k, t in st["trades"].items():
        if k.split("#")[0] == sym and "symbol" in t and not t.get("closed"):
            return True
    return False


def fmt_skipped(sym, side, name, etime, entry, tag="", why=None):
    """Alert for a master signal that is logged but NOT traded (policy rule).
    Sent once per signal; info-only."""
    why = why or f"{sym} trade already open — 1-open-trade-per-stock rule"
    return (f"{tag}⏭️ <b>NO NEW TRADE — {sym} {side}</b>\n"
            f"{name} @ {etime} · ₹{entry:,.2f}\n"
            f"🚦 {why}")


def engine_frame(hist_csv, today_bars, today):
    """hist_csv path + today's bars (RangeIndex, has 'dt','t') -> engine-ready df."""
    df = today_bars.set_index("dt")[["open", "high", "low", "close", "volume"]]
    if hist_csv.exists():
        h = pd.read_csv(hist_csv, parse_dates=["dt"])
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata") \
            if h["dt"].dt.tz is None else h["dt"]
        h = h[h["dt"].dt.strftime("%Y-%m-%d") != today].set_index("dt")
        df = pd.concat([h[["open", "high", "low", "close", "volume"]], df])
    df = df.sort_index()
    # Canonical tz-aware DatetimeIndex: CSV re-parse yields UTC+05:30 while live
    # feeds yield Asia/Kolkata — concat of differing tz dtypes becomes a plain
    # object Index (no .hour) and the engine crashes. Normalize via UTC.
    df.index = pd.to_datetime(df.index, utc=True).tz_convert("Asia/Kolkata")
    df.index.name = None
    return df


# ---------------------------------------------------------------- test
def mode_test():
    ok = True
    print("== secrets ==")
    for k in ("DHAN_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        print(f"  {k}: {'set' if os.environ.get(k) else 'MISSING'}")
    print("== engine self-test ==")
    r = subprocess.run([sys.executable, str(ROOT / "test_master_scanner.py")],
                       capture_output=True, text=True, cwd=ROOT)
    print("  engine:", "PASS" if "ALL CHECKS PASSED" in r.stdout else f"FAIL\n{r.stdout[-400:]}{r.stderr[-400:]}")
    ok &= "ALL CHECKS PASSED" in r.stdout
    print("== feeds ==")
    df = feeds.fetch_bars_yahoo("RELIANCE", "5d")
    print("  yahoo RELIANCE 5m bars:", 0 if df is None else len(df))
    ok &= df is not None and len(df) > 0
    if os.environ.get("DHAN_TOKEN"):
        try:
            dd = feeds.fetch_bars_dhan(SID["RELIANCE"],
                                       (now_ist() - dt.timedelta(days=3)).strftime("%Y-%m-%d 09:15:00"),
                                       now_ist().strftime("%Y-%m-%d %H:%M:%S"))
            print("  dhan RELIANCE bars:", 0 if dd is None else len(dd))
            ok &= dd is not None and len(dd) > 0
        except Exception as e:
            print("  dhan FAIL:", e)
            ok = False
    print("== end-to-end live path (history CSV + today's bars -> engine) ==")
    try:
        now = now_ist(); today = now.strftime("%Y-%m-%d")
        tb, tsrc = feeds.fetch_today("RELIANCE", SID["RELIANCE"], now)
        if tb is None or tb.empty:
            print("  skipped (no bars yet today — pre-market, OK)")
        else:
            tb = tb.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
            tb["t"] = tb["dt"].dt.strftime("%H:%M")
            dfe = engine_frame(HIST / "RELIANCE.csv", tb, today)
            res = ms.run_symbol(dfe, ms.Params(enable_buy_ex10=False, enable_buy_ex11=False))
            todays = res[res.index.strftime("%Y-%m-%d") == today]
            n_master = todays["scan_code"].dropna().astype(int).isin(MASTER_CODES).sum()
            print(f"  ok: {len(dfe)} bars ({len(todays)} today, src={tsrc}), master signals today: {n_master}")
    except Exception as e:
        print(f"  FAIL: {type(e).__name__}: {e}")
        ok = False
    print("== oi gate probe (nse live) ==")
    _, meta = gate.nse_live(SYMS)
    print(" ", meta)
    print("== telegram ==")
    tg.test()
    print("RESULT:", "PASS ✅" if ok else "FAIL ❌ (see above)")
    return 0 if ok else 1


# ---------------------------------------------------------------- bootstrap
def prev_oi_base(today_str):
    import io, zipfile, csv as _csv
    import requests
    out = {}
    try:
        d = dt.date.fromisoformat(today_str) - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        url = f"https://archives.nseindia.com/archives/fo/mkt/fo{d.strftime('%d%m%Y')}.zip"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=90)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        fut = [n for n in z.namelist() if n.startswith("fo") and n.endswith(".csv")][0]
        opt = [n for n in z.namelist() if n.startswith("op") and n.endswith(".csv")][0]
        for name, idx, inst in ((fut, 7, "FUTSTK"), (opt, 9, "OPTSTK")):
            for row in _csv.reader(io.TextIOWrapper(z.open(name))):
                if len(row) > idx and row[0].strip() == inst:
                    try:
                        out[row[1].strip()] = out.get(row[1].strip(), 0.0) + float(row[idx])
                    except ValueError:
                        pass
        pd.DataFrame([{"symbol": k, "oi_prev": v} for k, v in out.items()]).to_csv(
            ROOT / "data" / "oi_prev.csv", index=False)
        print(f"prev-day OI base: {len(out)} underlyings ({d})")
    except Exception as e:
        print("oi_prev fetch failed (non-fatal, gate uses nse-live):", e)


def mode_bootstrap(kind="full"):
    now = now_ist()
    since = (now - dt.timedelta(days=95)).strftime("%Y-%m-%d 09:15:00")
    if kind == "refresh-60d":
        since = (now - dt.timedelta(days=63)).strftime("%Y-%m-%d 09:15:00")
    to = now.strftime("%Y-%m-%d %H:%M:%S")
    HIST.mkdir(parents=True, exist_ok=True)
    n_ok = 0
    for sym in SYMS:
        df = None
        if os.environ.get("DHAN_TOKEN"):
            try:
                df = feeds.fetch_bars_dhan(SID[sym], since, to)
                time.sleep(0.6)
            except Exception as e:
                print(f"  dhan {sym}: {e}")
        if df is None or df.empty:
            df = feeds.fetch_bars_yahoo(sym, "60d")
            time.sleep(1.2)
        if df is None or df.empty:
            print(f"  !! {sym}: no data")
            continue
        df[["dt", "open", "high", "low", "close", "volume"]].to_csv(HIST / f"{sym}.csv", index=False)
        n_ok += 1
        if n_ok % 25 == 0:
            print(f"  {n_ok}/{len(SYMS)}")
    prev_oi_base(now.strftime("%Y-%m-%d"))
    print(f"bootstrap done: {n_ok}/{len(SYMS)} symbols")


# ---------------------------------------------------------------- live cycle
def mode_live():
    """Runs one scan cycle. Returns True if a cycle ran, False if idle (pre-market/EOD)."""
    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    alerts_before = len(st["alerts"])
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("EOD done — idle.")
        save_state(st)          # still write state so the commit step has a file
        return False
    if hhmm < "09:16":
        print("pre-market — idle.")
        save_state(st)          # still write state so the commit step has a file
        return False

    ranks, meta = gate.nse_live(SYMS)
    st["gate"] = meta
    st["gate_rank"] = {k: int(v) for k, v in ranks.items()}
    gate_ok = bool(ranks)
    print(f"gate: {meta}")

    # --- fetch today's bars once per symbol (one bad feed must never kill the cycle)
    bars_map = {}
    for sym in SYMS:
        try:
            b, src = feeds.fetch_today(sym, SID[sym], now)
            if b is not None and not b.empty:
                b = b.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
                b["t"] = b["dt"].dt.strftime("%H:%M")
                bars_map[sym] = b
        except Exception as e:
            print(f"  feed {sym}: {type(e).__name__}: {e}")
        time.sleep(0.15)
    if not bars_map:
        print("  WARNING: no feeds returned bars this cycle (dhan+yahoo both down?)")

    # --- manage open trades FIRST (deterministic re-eval; alert only NEW events).
    # Must run BEFORE the signal scan: with the 1-open-trade rule the scanner
    # needs 'closed' computed against the newest bars of this same cycle, so a
    # stop/target that printed on a bar frees the stock for a signal on that bar.
    for tkey in list(st["trades"].keys()):
        sym = tkey.split("#")[0]
        tbars = bars_map.get(sym)
        if tbars is None:
            continue
        tr = st["trades"][tkey]
        try:
            new_tr = trader.evaluate(sym, tr["side"], tr["time"], float(tr["entry"]), tr["signal"], tbars, warmup=trader.load_warmup(HIST / f"{sym}.csv", today))
            new_tr["gate_rank"] = tr.get("gate_rank")
            st["trades"][tkey] = new_tr
            for ev in new_tr["events"]:
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and key not in st["alerts"]:
                    tg.send_message(trader.fmt_alert(new_tr, ev["key"]))
                    st["alerts"].append(key)
        except Exception as e:
            print(f"  manage {sym}: {type(e).__name__}: {e}")

    # --- engine -> new master signals -> gate -> paper entry
    # CAUSAL evaluation (TradingView once-per-bar-close truth): every pending
    # today-bar is evaluated ONLY against data up to that bar's own close, i.e.
    # the engine is re-run on a frame truncated at the bar being evaluated.
    # Evaluating old rows on a grown dataset contaminates them with FUTURE
    # values (today's forming day-high/low, the still-filling 15-min bucket,
    # confirmed-vs-forming daily close) -> false signals + timing shifts.
    raw_new = 0
    skipped_open = 0
    skipped_early = 0
    skipped_table = 0
    skipped_quality = 0
    params = ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)  # user's chart toggles
    for sym, tbars in bars_map.items():
        n_today = len(tbars)
        known = int(st["signals"].get(sym, {}).get("nbars", 0))
        if known > n_today:
            known = 0                                # feed shape changed - rescan today
        for j in range(known, n_today):
            t_bar = tbars["dt"].iloc[j]
            try:
                tk = pd.Timestamp(t_bar)                      # forming-bar guard: evaluate
                if tk.tzinfo is None:                         # only CLOSED bars
                    tk = tk.tz_localize(now.tz)
                if tk + pd.Timedelta(minutes=5) > pd.Timestamp(now):
                    break
                df = engine_frame(HIST / f"{sym}.csv", tbars.iloc[: j + 1], today)
                res = ms.run_symbol(df, params)
                row = res.iloc[-1]
            except Exception as e:
                print(f"  engine {sym}: {e}")
                break                                   # keep pointer, retry next cycle
            try:
                code = row.get("scan_code")
                if not pd.isna(code) and int(code) in MASTER_CODES:
                    side = "BUY" if int(code) < 200 else "SELL"
                    etime = tk.strftime("%H:%M")
                    entry = float(tbars["close"].iloc[j])     # signal-bar close = chart truth
                    rank = ranks.get(sym)
                    if int(code) in (90, 290):
                        print(f"  {sym} {side} @ {etime} — SKIPPED: scanner-table preview 90/290 (no chart label)")
                        tg.send_message(fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                    why="scanner-table preview (ENTRY BUY/SELL) — appears only in the TradingView scanner table, never as a chart label"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "scanner-table preview signal (90/290) — no TradingView chart label, blocked"})
                        skipped_table += 1
                        time.sleep(0.5)
                    elif int(code) in EX_WEAK_CODES:
                        print(f"  {sym} {side} @ {etime} — SKIPPED: weak EX variant (EX9+) quality filter")
                        tg.send_message(fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                    why="weak exception variant (EX9 or higher) — quality filter: EX9+ lost money in the 2-day review; only EX1-EX8 + NORMAL signals are traded"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "weak EX variant (EX9+) blocked — B2 quality filter"})
                        skipped_quality += 1
                        time.sleep(0.5)
                    elif int(code) in EX_RAZOR_CODES and etime < EX_OPEN_FROM:
                        print(f"  {sym} {side} @ {etime} — SKIPPED: EX1/EX2 at open (razor class, allowed from {EX_OPEN_FROM})")
                        tg.send_message(fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                    why=f"fresh EX1/EX2 at the open — razor-edge class (Dhan-vs-TradingView tick flips; biggest loser group in review); allowed from {EX_OPEN_FROM}"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": f"EX1/EX2 before {EX_OPEN_FROM} blocked — razor-edge class (B2)"})
                        skipped_quality += 1
                        time.sleep(0.5)
                    elif etime < CHART_MIN_TIME:
                        print(f"  {sym} {side} @ {etime} — SKIPPED: chart window (<{CHART_MIN_TIME})")
                        tg.send_message(fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry,
                                                    why=f"before {CHART_MIN_TIME} chart window — not on TradingView yet"))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": f"signal before {CHART_MIN_TIME} chart window (not on TradingView)"})
                        skipped_early += 1
                        time.sleep(0.5)
                    elif not gate_ok:
                        print(f"  {sym} {side} @ {etime} — GATE OFFLINE: logged, no trade (strict)")
                        tg.send_message(fmt_raw_signal(sym, side, str(row.get("scan_name", code)),
                                                       etime, entry, None, False))
                        raw_new += 1
                        time.sleep(0.5)
                    elif rank is None or rank > 30:
                        print(f"  {sym} {side} @ {etime} — spurt rank {rank}, filtered")
                        tg.send_message(fmt_raw_signal(sym, side, str(row.get("scan_name", code)),
                                                       etime, entry, rank, True))
                        raw_new += 1
                        time.sleep(0.5)
                    elif sym_has_open(st, sym):
                        print(f"  {sym} {side} @ {etime} — SKIPPED: {sym} trade already open (1-open-trade rule)")
                        tg.send_message(fmt_skipped(sym, side, str(row.get("scan_name", code)), etime, entry))
                        st.setdefault("skipped", []).append(
                            {"symbol": sym, "side": side, "signal": str(row.get("scan_name", code)),
                             "time": etime, "entry": round(entry, 2), "why": "open position already on stock (1-open-trade rule)"})
                        skipped_open += 1
                        time.sleep(0.5)
                    else:
                        tr = trader.evaluate(sym, side, etime, entry, str(row.get("scan_name", code)), tbars, warmup=trader.load_warmup(HIST / f"{sym}.csv", today))
                        if "error" in tr:
                            print(f"  {sym} {side} @ {etime} — trader rejected: {tr.get('error')}")
                        else:
                            tr["gate_rank"] = int(rank)
                            tkey, k = sym, 2
                            while tkey in st["trades"]:       # one paper trade per signal,
                                tkey = f"{sym}#{k}"; k += 1   # keyed like the chart's labels
                            st["trades"][tkey] = tr
                            st["alerts"].append(f"{tkey}:ENTRY")
                            suffix = f" · #{k-1} on {sym}" if tkey != sym else ""
                            tg.send_message(trader.fmt_alert(tr, "ENTRY") +
                                            f"\n🏆 NSE OI-spurt rank #{rank}{suffix}")
                            print(f"  >>> ENTRY {tkey} {side} @ {entry} (rank {rank})")
            except Exception as e:
                print(f"  signals {sym}: {type(e).__name__}: {e}")
            st["signals"][sym] = st["signals"].get(sym, {})
            st["signals"][sym]["nbars"] = j + 1

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        try:
            done = [t for t in st["trades"].values() if "symbol" in t]
            dlbl = now.strftime("%d-%b-%Y")
            sk = {}
            for it in st.get("skipped", []):
                sk.setdefault(it["why"], []).append([it["symbol"], it["side"], it["signal"], it["time"], it["entry"]])
            out = report.build(done, dlbl, st["gate"], str(ROOT / f"paper_test_{today}.xlsx"), skipped=sk or None)
            tg.send_message(report.summary_text(done, dlbl, st["gate"]))
            tg.send_document(out, caption=f"📄 Paper test report {today}")
            st["eod_done"] = True
        except Exception as e:
            print(f"  EOD report: {type(e).__name__}: {e}")

    # --- per-cycle silent status line: opening Telegram always shows when the
    #     engine last ran (no sound/notification). Real alerts stay loud.
    new_tg = (len(st["alerts"]) - alerts_before) + raw_new
    if "09:20" <= hhmm < "15:26":
        tg.send_message(f"💓 {hhmm} IST · {len(st['trades'])} trades · alerts {len(st['alerts'])} · "
                        f"+{new_tg} this cycle · gate {'OK' if gate_ok else 'OFFLINE'}", silent=True)

    st["cycles"] += 1
    save_state(st)
    print(f"cycle done: {len(st['trades'])} trades · {len(bars_map)} symbols fed · "
          f"TG alerts today: {len(st['alerts'])} (+{len(st['alerts']) - alerts_before}, raw +{raw_new}, "
          f"skipped-open +{skipped_open}, skip-<09:26 +{skipped_early}, skip-table +{skipped_table}, skip-qual +{skipped_quality}) · gate {'OK' if gate_ok else 'OFFLINE'}")
    return True


# ---------------------------------------------------------------- replay
def mode_replay(date, trade_csv):
    trades = []
    for _, r in pd.read_csv(trade_csv).iterrows():
        sym = r["symbol"]
        fp = HIST / f"{sym}.csv"
        if not fp.exists():
            print("missing bars", sym); continue
        b = pd.read_csv(fp, parse_dates=["dt"])
        b["dt"] = pd.to_datetime(b["dt"], utc=True).dt.tz_convert("Asia/Kolkata") if b["dt"].dt.tz is None else b["dt"]
        b = b[b["dt"].dt.strftime("%Y-%m-%d") == date].reset_index(drop=True)
        b["t"] = b["dt"].dt.strftime("%H:%M")
        tr = trader.evaluate(sym, r["side"], r["time"], float(r["entry"]), r.get("signal", ""), b,
                         warmup=trader.load_warmup(fp, date))
        tr["gate_rank"] = int(r["spurt_rank"]) if "spurt_rank" in r and pd.notna(r["spurt_rank"]) else "-"
        trades.append(tr)
        for ev in tr["events"]:
            print("  ALERT:", trader.fmt_alert(tr, ev["key"]).replace("<b>", "").replace("</b>", ""))
    if trades:
        out = report.build(trades, date, {"status": "REPLAY", "source": "cached"}, str(ROOT / "replay_report.xlsx"))
        print("\n" + report.summary_text(trades, date, {"status": "REPLAY", "source": "cached"}))
        print("report:", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--bootstrap", nargs="?", const="full", choices=["full", "refresh-60d"])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--loop", type=int, default=1, help="run N consecutive live cycles (~4 min apart)")
    ap.add_argument("--replay")
    ap.add_argument("--trades-csv", default="replay_trades.csv")
    a = ap.parse_args()
    if a.test:
        sys.exit(mode_test())
    elif a.bootstrap:
        mode_bootstrap(a.bootstrap)
    elif a.live:
        for i in range(max(1, a.loop)):
            active = mode_live()
            if not active:
                break
            if i < a.loop - 1:
                print(f"--- loop: cycle {i + 2} of {a.loop} in ~240s ---")
                time.sleep(240)
    elif a.replay:
        mode_replay(a.replay, a.trades_csv)
    else:
        ap.print_help()
