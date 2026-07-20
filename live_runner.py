#!/usr/bin/env python3
"""Forward paper-test runner — MASTER SECTOR BATCH SCANNER (100% intact engine)
+ TOP-30 strong OI-spurt gate + ₹50k paper sizing + Telegram alerts.

Modes:
  --test                connectivity/self test (secrets, feeds, engine, telegram)
  --bootstrap           download ~90d history for all 210 stocks -> data/history/
                        (+ prev-day combined OI base via NSE fo bhavcopy, best effort)
  --live                one cycle: fetch today's bars, run engine, gate, trade, alert
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

import master_scanner as ms
import feeds, gate, trader, report, telegram_bot as tg

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STATE = ROOT / "state.json"
HIST = ROOT / "data" / "history"
MASTER_CODES = set(range(101, 113)) | set(range(201, 221)) | {80, 90, 280, 290}

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


def engine_frame(hist_csv, today_bars, today):
    """hist_csv path + today's bars (RangeIndex, has 'dt','t') -> engine-ready df."""
    df = today_bars.set_index("dt")[["open", "high", "low", "close", "volume"]]
    if hist_csv.exists():
        h = pd.read_csv(hist_csv, parse_dates=["dt"])
        h["dt"] = pd.to_datetime(h["dt"], utc=True).dt.tz_convert("Asia/Kolkata") \
            if h["dt"].dt.tz is None else h["dt"]
        h = h[h["dt"].dt.strftime("%Y-%m-%d") != today].set_index("dt")
        df = pd.concat([h[["open", "high", "low", "close", "volume"]], df])
    return df.sort_index()


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
    now = now_ist()
    today = now.strftime("%Y-%m-%d")
    st = load_state(today)
    hhmm = now.strftime("%H:%M")
    if st["eod_done"]:
        print("EOD done — idle.")
        save_state(st)          # still write state so the commit step has a file
        return
    if hhmm < "09:16":
        print("pre-market — idle.")
        save_state(st)          # still write state so the commit step has a file
        return

    ranks, meta = gate.nse_live(SYMS)
    st["gate"] = meta
    st["gate_rank"] = {k: int(v) for k, v in ranks.items()}
    gate_ok = bool(ranks)
    print(f"gate: {meta}")

    # --- fetch today's bars once per symbol
    bars_map = {}
    for sym in SYMS:
        b, src = feeds.fetch_today(sym, SID[sym], now)
        if b is not None and not b.empty:
            b = b.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
            b["t"] = b["dt"].dt.strftime("%H:%M")
            bars_map[sym] = b
        time.sleep(0.15)

    # --- engine -> new master signals -> gate -> paper entry
    params = ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)  # user's chart toggles
    for sym, tbars in bars_map.items():
        try:
            df = engine_frame(HIST / f"{sym}.csv", tbars, today)
            res = ms.run_symbol(df, params)
        except Exception as e:
            print(f"  engine {sym}: {e}")
            continue
        today_res = res[res.index.strftime("%Y-%m-%d") == today]
        known = st["signals"].get(sym, {}).get("nbars", 0)
        fired = False
        for idx, row in today_res.iloc[known:].iterrows():
            code = row.get("scan_code")
            if pd.isna(code) or int(code) not in MASTER_CODES:
                continue
            side = "BUY" if int(code) < 200 else "SELL"
            etime = idx.strftime("%H:%M")
            entry = float(row["close"])
            rank = ranks.get(sym)
            if not gate_ok:
                print(f"  {sym} {side} @ {etime} — GATE OFFLINE: logged, no trade (strict)")
                fired = True; break
            if rank is None or rank > 30:
                print(f"  {sym} {side} @ {etime} — spurt rank {rank}, filtered")
                fired = True; break
            tr = trader.evaluate(sym, side, etime, entry, str(row.get("scan_name", code)), tbars)
            tr["gate_rank"] = int(rank)
            st["trades"][sym] = tr
            st["alerts"].append(f"{sym}:ENTRY")
            tg.send_message(trader.fmt_alert(tr, "ENTRY") + f"\n🏆 NSE OI-spurt rank #{rank}")
            print(f"  >>> ENTRY {sym} {side} @ {entry} (rank {rank})")
            fired = True; break
        st["signals"][sym] = {"nbars": len(today_res)} if not fired else st["signals"].get(sym, {"nbars": len(today_res)})
        st["signals"][sym]["nbars"] = len(today_res)

    # --- manage open trades (deterministic re-eval; alert only NEW events)
    for sym in list(st["trades"].keys()):
        tbars = bars_map.get(sym)
        if tbars is None:
            continue
        tr = st["trades"][sym]
        new_tr = trader.evaluate(sym, tr["side"], tr["time"], float(tr["entry"]), tr["signal"], tbars)
        new_tr["gate_rank"] = tr.get("gate_rank")
        st["trades"][sym] = new_tr
        for ev in new_tr["events"]:
            key = f"{sym}:{ev['key']}"
            if ev["key"] != "ENTRY" and key not in st["alerts"]:
                tg.send_message(trader.fmt_alert(new_tr, ev["key"]))
                st["alerts"].append(key)

    # --- EOD report at/after 15:25
    if hhmm >= "15:25":
        done = [t for t in st["trades"].values() if "symbol" in t]
        dlbl = now.strftime("%d-%b-%Y")
        out = report.build(done, dlbl, st["gate"], str(ROOT / f"paper_test_{today}.xlsx"))
        tg.send_message(report.summary_text(done, dlbl, st["gate"]))
        tg.send_document(out, caption=f"📄 Paper test report {today}")
        st["eod_done"] = True

    st["cycles"] += 1
    save_state(st)
    print(f"cycle done: {len(st['trades'])} trades · {len(bars_map)} symbols fed · gate "
          f"{'OK' if gate_ok else 'OFFLINE'}")


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
        tr = trader.evaluate(sym, r["side"], r["time"], float(r["entry"]), r.get("signal", ""), b)
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
    ap.add_argument("--replay")
    ap.add_argument("--trades-csv", default="replay_trades.csv")
    a = ap.parse_args()
    if a.test:
        sys.exit(mode_test())
    elif a.bootstrap:
        mode_bootstrap(a.bootstrap)
    elif a.live:
        mode_live()
    elif a.replay:
        mode_replay(a.replay, a.trades_csv)
    else:
        ap.print_help()
