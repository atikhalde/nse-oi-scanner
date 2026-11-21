#!/usr/bin/env python3
"""Independent M16 continuation alerts. Shared M6 feed; dedicated bot/state.

Best-effort polling is NOT a tick feed. Runtime and upstream publication delay
are reported, never disguised as an exact one-second execution guarantee.
"""
from __future__ import annotations
import argparse
import html
import json
import os
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from pprint import pformat

import numpy as np
import pandas as pd
import m6_runner as shared
import master_scanner as scanner
import telegram_bot as tg
import m16_model as model

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state16.json"
MAX_LAG = 30


def credentials():
    # Deliberately no fallback to any M2/M6/default bot.
    token, chat = os.getenv("M16_BOT_TOKEN", "").strip(), os.getenv("M16_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("Configure dedicated M16_BOT_TOKEN and M16_CHAT_ID repository secrets; no shared bot fallback")
    return token, chat


def telegram_targets():
    """Primary required; optional mirror never falls back to another model."""
    targets = [credentials()]
    token = os.getenv("M16_BOT_TOKEN_2", "").strip()
    chat = os.getenv("M16_CHAT_ID_2", "").strip()
    if bool(token) != bool(chat):
        print("M16 mirror incomplete: configure both M16_BOT_TOKEN_2 and M16_CHAT_ID_2; mirror ignored")
    elif token and (token, chat) not in targets:
        targets.append((token, chat))
    return targets


def target_id(token, chat):
    # No tokens/chat IDs in persisted state or TXT reports.
    import hashlib
    return hashlib.sha256((token + "\0" + chat).encode()).hexdigest()[:24]


def deliver_targets(st, ledger, sender, can_send=lambda: True):
    """Persist per-destination acknowledgements; retry failed targets only.

    `sending` after a crash is uncertain and is not blindly resent. This does
    not prevent delivery/retry to the OTHER destination.
    """
    targets = telegram_targets()
    for token, chat in targets:
        key = target_id(token, chat)
        if ledger.get(key) in {"sent", "sending"}:
            continue
        if not can_send():
            ledger[key] = "expired_unsent"
            save(st)
            continue
        ledger[key] = "sending"
        save(st)
        try:
            ledger[key] = "sent" if sender(token, chat) else "failed"
        except Exception:
            ledger[key] = "failed"
            print("M16 delivery failed for one destination; other targets still attempted")
        save(st)
    return all(ledger.get(target_id(*target)) == "sent" for target in targets)


def test_alert(dry_run=False):
    if dry_run:
        print("[DRY] M16 dedicated-bot self-test")
        return True
    results = []
    for token, chat in telegram_targets():
        try:
            results.append(bool(tg.send_message_to(token, chat,
                "M16 bot connected — continuation alerts only; no orders.")))
        except Exception:
            results.append(False)
    print(f"M16 self-test: {sum(results)}/{len(results)} destination(s) confirmed")
    return all(results)


def load(day):
    if not STATE.exists():
        return model.fresh_state(day)
    st = json.loads(STATE.read_text(encoding="utf-8"))
    if not isinstance(st, dict) or "date" not in st:
        raise ValueError("Invalid M16 state; refusing to overwrite")
    if st["date"] != day:
        return model.fresh_state(day)
    for key, default in model.fresh_state(day).items():
        if key not in st:
            st[key] = default
        elif type(st[key]) is not type(default):
            raise ValueError(f"Invalid M16 state field: {key}; refusing to overwrite")
    return st


def save(st, dry_run=False):
    if not dry_run:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        tmp.replace(STATE)


def normalize(bars, day):
    b = shared._prep(bars)
    if b is None:
        return None
    b = shared._today_only(b, day)
    b["volume"] = pd.to_numeric(b["volume"], errors="coerce").replace([np.inf, -np.inf], 0).fillna(0).clip(lower=0)
    for col in ("open", "high", "low", "close"):
        b[col] = pd.to_numeric(b[col], errors="coerce")
    mask = np.isfinite(b[["open", "high", "low", "close"]]).all(axis=1)
    mask &= (b[["open", "high", "low", "close"]] > 0).all(axis=1)
    mask &= (b["high"] >= b[["open", "close", "low"]].max(axis=1))
    mask &= (b["low"] <= b[["open", "close", "high"]].min(axis=1))
    mask &= (b["dt"].dt.minute % 5 == 0) & (b["dt"].dt.second == 0)
    mask &= b["t"].between("09:15", "15:25")
    return b[mask].reset_index(drop=True)


def analyze(job):
    """Independent worker: causal indicator evaluation, every EX switch enabled.

    Only the current prefix's last row can create a seed. In particular a
    full-day engine run is never used to infer historical labels.
    """
    symbol, bars, cache, cursor, now = job
    closed = shared._closed_bars(bars, now)
    if closed is None or closed.empty or not cache.get("ok"):
        return symbol, []
    output = []
    for i, row in closed.iterrows():
        t = model.stamp(row["dt"])
        if cursor and t <= model.stamp(cursor):
            continue
        prefix = closed.iloc[:i+1]
        frame = shared.engine_frame(cache, prefix)
        ema9 = frame["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        ema10 = frame["close"].ewm(span=10, adjust=False).mean().iloc[-1]
        code, name = None, ""
        if model.before(t + pd.Timedelta(minutes=5), "11:00:00"):
            res = scanner.run_symbol(frame, scanner.Params())
            if res is not None and not res.empty and pd.notna(res.iloc[-1].get("scan_code")):
                code = int(res.iloc[-1]["scan_code"])
                name = str(res.iloc[-1].get("scan_name", code))
        bar = {k: float(row[k]) for k in ("open", "high", "low", "close", "volume")}
        bar.update(dt=t.isoformat(), ema9=float(ema9), ema10=float(ema10))
        context = prefix.tail(5)[["low", "high"]].to_dict("list")
        output.append((bar, code, name, context))
    return symbol, output


def apply(st, symbol, rows, now):
    for bar, code, name, context in rows:
        if st["cursors"].get(symbol) and model.stamp(bar["dt"]) <= model.stamp(st["cursors"][symbol]):
            continue
        model.closed_bar(st, symbol, bar, now, MAX_LAG)
        if code is not None:
            model.seed(st, symbol, code, name, bar, pd.DataFrame(context))
        st["cursors"][symbol] = bar["dt"]


def observe_running(st, symbol, bars, now):
    """Process prior closes FIRST; never mislabel an old break as a new one."""
    if bars is None or bars.empty:
        return
    current = bars.iloc[-1]
    cursor = st["cursors"].get(symbol)
    required = model.stamp(current["dt"])-pd.Timedelta(minutes=5)
    if not cursor or model.stamp(cursor) < required:
        return
    model.running_bar(st, symbol, current, now)


def alert_text(e):
    labels = dict(RUNNING_BREAK="RUNNING BOS — unconfirmed",
                  CLOSE_CONFIRMED="CANDLE CLOSE — BOS CONFIRMED",
                  CLOSE_FAILED="CANDLE CLOSE — BOS FAILED (not an entry)",
                  CLOSE_ONLY="CANDLE CLOSE — BOS confirmed; running crossing was not observed")
    return (f"M16 · {labels.get(e['kind'], e['kind'])}\n{e['symbol']} {e['side']} @ {e['price']:.2f}\n"
            f"BOS {e['bos']:.2f} · pullback {e['pullback']}\n"
            f"Original {e['signal']} candle {model.stamp(e['seed']).strftime('%H:%M')}\n"
            f"Breakout candle {model.stamp(e['bar']).strftime('%H:%M')} · observed {model.stamp(e['observed']).strftime('%H:%M:%S')} IST\n"
            "Feed-polled observation, not a guaranteed tick-time fill. Alerts only; no live order.")


def event_fresh(event, now):
    now = model.stamp(now)
    start = model.stamp(event["bar"])
    age = (now-model.stamp(event["observed"])).total_seconds()
    if now.strftime("%Y-%m-%d") != start.strftime("%Y-%m-%d") or not 0 <= age <= MAX_LAG:
        return False
    end = start+pd.Timedelta(minutes=5)
    if event["kind"] == "RUNNING_BREAK":
        return start <= now < end  # NEVER send a provisional alert after close
    return 0 <= (now-end).total_seconds() <= MAX_LAG


def flush(st, now, dry_run=False):
    """Durable per-event outbox. Uncertain crash sends are not blindly repeated."""
    for item in st["outbox"].values():
        if item["status"] not in {"pending", "failed"}:
            continue
        if item.get("retry_after") and model.stamp(now) < model.stamp(item["retry_after"]):
            continue
        e = item["event"]
        if not event_fresh(e, now):
            item["status"] = "expired_unsent"
            continue
        if dry_run:
            print("[DRY] " + alert_text(e))
            item["status"] = "dry_run"
            continue
        item["status"] = "failed"  # per-target ledger protects acknowledged sends
        ledger = item.setdefault("deliveries", {})
        # Recheck between bot sends too: a slow/failing first bot must not
        # make the mirror send an already-closed "running candle" alert.
        started = time.monotonic()
        delivered = deliver_targets(st, ledger, lambda token, chat:
                                    tg.send_message_to(token, chat, html.escape(alert_text(e))),
                                    can_send=lambda: event_fresh(e, model.stamp(now) +
                                        pd.Timedelta(seconds=time.monotonic()-started)))
        item["status"] = "sent" if delivered else "failed"
        if item["status"] == "failed":
            item["retry_after"] = (model.stamp(now) + pd.Timedelta(seconds=3)).isoformat()
        save(st)


def finish(st, dry_run=False):
    if st.get("eod_done"):
        return
    text = (f"M16 CONTINUATION REVIEW — {st['date']}\n"
            f"Setups: {len(st['setups'])}\nPhases: {dict(Counter(s['phase'] for s in st['setups'].values()))}\n"
            f"Events: {dict(Counter(e['kind'] for e in st['events']))}\n"
            "Running alerts are provisional; failed closes are NOT confirmed entries.\n"
            "Historical replay events are recorded, not sent. No hypothetical P&L.\n\n" + pformat(st, width=120))
    if dry_run:
        print("[DRY] M16 TXT review")
        return
    report = STATE.parent / f"activity_M16_{st['date']}.txt"
    report.write_text(text, encoding="utf-8")
    ledger = st.setdefault("eod_deliveries", {})
    if deliver_targets(st, ledger, lambda token, chat: tg.send_document_to(
            token, chat, str(report), caption=f"M16 continuation review {st['date']}")):
        st["eod_done"] = True
        save(st)


def cycle(st, now, bars_map, cache, pool=None, dry_run=False):
    """Injectable offline surface; dispatch each symbol result as it finishes."""
    jobs = []
    for sym, raw in bars_map.items():
        bars = normalize(raw, st["date"])
        if bars is None or bars.empty:
            continue
        if not cache.get(sym, {}).get("ok"):
            st["diagnostics"].append(dict(at=model.stamp(now).isoformat(), symbol=sym, reason="history unavailable"))
            continue
        jobs.append((sym, bars, cache[sym], st["cursors"].get(sym), now))
    if pool:
        futures = {pool.submit(analyze, j): j[0] for j in jobs}
        results = ((futures[f], f) for f in as_completed(futures))
    else:
        results = ((j[0], j) for j in jobs)
    for sym, task in results:
        observed = shared.now_ist() if pool else now
        try:
            _, rows = task.result() if pool else analyze(task)
            apply(st, sym, rows, observed)
            bars = normalize(bars_map[sym], st["date"])
            if bars is not None and not bars.empty:
                observe_running(st, sym, bars, observed)
            save(st, dry_run)
            flush(st, observed, dry_run)
        except Exception as exc:
            st["diagnostics"].append(dict(at=model.stamp(observed).isoformat(), symbol=sym,
                                          reason=f"{type(exc).__name__}: {exc}"))
            print(f"M16 {sym}: {type(exc).__name__}: {exc}")
    model.expire(st, now)
    st["diagnostics"] = st["diagnostics"][-2000:]
    save(st, dry_run)


def timed_fetch(sym):
    _, bars = shared._fetch_symbol(sym, shared.now_ist())
    return bars, shared.now_ist()


def live_session(st, cache, max_minutes, dry_run=False):
    """Non-blocking discovery: monitoring never waits for a universe-wide batch.

    Separate active-analysis workers keep the morning seed-discovery queue from
    starving existing setups. Network requests still have upstream latency.
    """
    started, last_save = time.monotonic(), 0.0
    poll = max(1., float(os.getenv("M16_POLL_SECONDS", "1")))
    last_discovery, fetches, engines, latest, requested = None, {}, {}, {}, {}
    worker_count = max(1, int(os.getenv("M16_ENGINE_WORKERS", "4")))
    with ThreadPoolExecutor(max_workers=16) as fetch_pool, ThreadPoolExecutor(max_workers=8) as watch_fetch, \
            ProcessPoolExecutor(max_workers=worker_count) as discovery, ProcessPoolExecutor(max_workers=2) as active_pool:
        while time.monotonic()-started < max_minutes*60:
            now = shared.now_ist()
            if now.strftime("%Y-%m-%d") != st["date"]:
                break
            if not model.before(now, "12:30:30"):
                model.expire(st, now)
                finish(st, dry_run)
                break
            active = {s["symbol"] for s in st["setups"].values() if s["phase"] not in model.TERMINAL}
            bucket = (model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")
            scan = last_discovery != bucket and model.before(now, "11:00:00")
            symbols = set(shared.L.SYMS) if scan else set()
            if scan:
                last_discovery = bucket
            # Re-fetch missing/late discovery bars during the freshness budget.
            # A batch arriving with yesterday's/previous candle's data must not
            # silently lose a morning signal until the next full discovery.
            if model.before(now, "11:00:00") and (model.stamp(now)-bucket).total_seconds() <= MAX_LAG:
                target = bucket-pd.Timedelta(minutes=5)
                for sym in shared.L.SYMS:
                    cached = latest.get(sym)
                    c = shared._closed_bars(cached[0], now) if cached else None
                    if c is None or c.empty or model.stamp(c.iloc[-1]["dt"]) < target:
                        symbols.add(sym)
            symbols |= active
            busy_fetch = set(fetches.values())
            for sym in sorted(symbols, key=lambda s: (s not in active, s)):
                interval = poll if sym in active else max(5., poll)
                if sym not in busy_fetch and time.monotonic()-requested.get(sym, -1e10) >= interval:
                    executor = watch_fetch if sym in active else fetch_pool
                    fetches[executor.submit(timed_fetch, sym)] = sym
                    requested[sym] = time.monotonic()
            for future in list(fetches):
                if not future.done():
                    continue
                sym = fetches.pop(future)
                try:
                    raw, received = future.result()
                    bars = normalize(raw, st["date"])
                    if bars is not None and not bars.empty:
                        latest[sym] = (bars, received)
                    else:
                        raise ValueError("current-session feed unavailable")
                except Exception as exc:
                    st["diagnostics"].append(dict(at=now.isoformat(), symbol=sym, reason=str(exc)))
            for future in list(engines):
                if not future.done():
                    continue
                sym = engines.pop(future)
                try:
                    _, rows = future.result()
                    apply(st, sym, rows, shared.now_ist())
                except Exception as exc:
                    st["diagnostics"].append(dict(at=now.isoformat(), symbol=sym, reason=f"engine: {exc}"))
            busy_engine = set(engines.values())
            for sym, (bars, received) in latest.items():
                observed = shared.now_ist()
                if (observed-received).total_seconds() <= 5:
                    observe_running(st, sym, bars, observed)
                closed = shared._closed_bars(bars, observed)
                if closed is None or closed.empty or sym in busy_engine:
                    continue
                cursor = st["cursors"].get(sym)
                if cursor and model.stamp(closed.iloc[-1]["dt"]) <= model.stamp(cursor):
                    continue
                if not cache.get(sym, {}).get("ok"):
                    continue
                executor = active_pool if sym in active else discovery
                engines[executor.submit(analyze, (sym, bars, cache[sym], cursor, observed))] = sym
            flush(st, shared.now_ist(), dry_run)
            model.expire(st, shared.now_ist())
            st["diagnostics"] = st["diagnostics"][-2000:]
            if time.monotonic()-last_save >= 2:
                save(st, dry_run)
                last_save = time.monotonic()
            time.sleep(.1)
        save(st, dry_run)
        for future in list(engines) + list(fetches):
            future.cancel()


def replay(day, symbols):
    """Disk-only causal replay. Never infers tick timing or sends Telegram."""
    st = model.fresh_state(day)
    end = pd.Timestamp(day + " 15:30", tz=model.IST)
    for sym in symbols:
        path = shared.L.HIST / f"{sym}.csv"
        if not path.exists():
            st["diagnostics"].append(dict(symbol=sym, reason="history file missing"))
            continue
        bars = normalize(pd.read_csv(path), day)
        if bars is None or bars.empty:
            continue
        cache = shared.symbol_history(sym, day)
        _, rows = analyze((sym, bars, cache, None, end))
        apply(st, sym, rows, end)
    model.expire(st, end)
    return st


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--max-runtime-min", type=float, default=205)
    parser.add_argument("--replay-date", help="Offline YYYY-MM-DD disk replay; no bot or feed calls")
    parser.add_argument("--symbols", nargs="+", default=None, help="Optional replay symbol subset")
    args = parser.parse_args()
    if args.replay_date:
        result = replay(args.replay_date, args.symbols or list(shared.L.SYMS))
        print(pformat(result, width=120))
        return
    if not args.dry_run:
        credentials()
    if args.test_alert:
        if not test_alert(args.dry_run):
            raise SystemExit("M16 test alert failed for one or more destinations")
        return
    now = shared.now_ist()
    if now.weekday() >= 5 or now.strftime("%H:%M") < "09:15" or now.strftime("%H:%M") >= "12:31":
        print("M16 outside weekday 09:15–12:31 IST window; no replay alerts")
        return
    st = load(now.strftime("%Y-%m-%d"))
    if st.get("eod_done"):
        return
    cache = {sym: shared.symbol_history(sym, st["date"]) for sym in shared.L.SYMS}
    missing_history = [sym for sym, rec in cache.items() if not rec.get("ok")]
    if missing_history:
        st["diagnostics"].append(dict(at=now.isoformat(), reason="history unavailable", symbols=missing_history))
        print(f"M16 history unavailable for {len(missing_history)} symbols; recorded in diagnostics")
    if args.session:
        live_session(st, cache, args.max_runtime_min, args.dry_run)
    else:
        bars_map = shared.fetch_symbols(list(shared.L.SYMS), now)
        cycle(st, now, bars_map, cache, dry_run=args.dry_run)
        if not model.before(shared.now_ist(), "12:30:30"):
            finish(st, args.dry_run)


if __name__ == "__main__":
    main()
