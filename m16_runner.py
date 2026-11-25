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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from pprint import pformat

import numpy as np
import pandas as pd
import m6_runner as shared
import master_scanner as scanner
import telegram_bot as tg
import m16_model as model
import m16_feed
import m16_calendar
import m16_sector

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
        # Closed session context only; includes pre-label pattern evidence for
        # original signals that first appear on the BOS breakout itself.
        pattern = prefix.tail(24).copy()
        pattern["ema9"] = frame["close"].ewm(span=9, adjust=False).mean().tail(len(pattern)).to_numpy()
        pattern["ema10"] = frame["close"].ewm(span=10, adjust=False).mean().tail(len(pattern)).to_numpy()
        pattern["dt"] = pattern["dt"].map(lambda v: model.stamp(v).isoformat())
        context = pattern[["dt", "open", "high", "low", "close", "volume", "ema9", "ema10"]].to_dict("list")
        output.append((bar, code, name, context))
    return symbol, output


def phase_snapshot(st):
    return {key: s.get("phase") for key,s in st["setups"].items()}


def record_transitions(st, before, now, source):
    for key,s in st["setups"].items():
        if before.get(key) != s.get("phase"):
            st.setdefault("transitions", []).append(dict(
                at=model.stamp(now).isoformat(), source=source, setup=key, symbol=s["symbol"],
                previous=before.get(key), phase=s.get("phase"),
                reason=s.get("invalid_reason", s.get("waiting_reason", "")),
                bos=s.get("bos"), rally=s.get("rally_evidence", {})))


def expire_setups(st, now):
    previous = phase_snapshot(st)
    model.expire(st, now)
    record_transitions(st, previous, now, "deadline")


def trim_diagnostics(st):
    excess = max(0, len(st["diagnostics"])-2000)
    if excess:
        st["diagnostics_omitted"] = st.get("diagnostics_omitted", 0)+excess
        st["diagnostics"] = st["diagnostics"][-2000:]


def apply(st, symbol, rows, now):
    for bar, code, name, context in rows:
        if st["cursors"].get(symbol) and model.stamp(bar["dt"]) <= model.stamp(st["cursors"][symbol]):
            continue
        previous = phase_snapshot(st)
        # Preserve all processed completed candles, including post-cutoff
        # observations. No forming-bar history is invented from final OHLC.
        st.setdefault("audit_bars", {}).setdefault(symbol, {})[bar["dt"]] = dict(bar)
        model.closed_bar(st, symbol, bar, now, MAX_LAG)
        if code is not None:
            key = model.seed(st, symbol, code, name, bar, pd.DataFrame(context))
            if key:
                model.direct_original_bos(st, key, bar, pd.DataFrame(context), now, MAX_LAG)
            setup = st["setups"].get(key, {})
            st.setdefault("signal_audit", []).append(dict(
                symbol=symbol, bar=bar["dt"], code=code, signal=name, setup=key,
                observed=model.stamp(now).isoformat(),
                historical=(model.stamp(now)-model.stamp(bar["dt"])-pd.Timedelta(minutes=5)).total_seconds() > MAX_LAG,
                decision=setup.get("phase", "excluded"),
                reason=setup.get("invalid_reason", "original signal confirmed directly on BOS" if setup.get("direct_bos") else
                                 "tracked final label" if key else "preview/unsupported code or outside seed window")))
        record_transitions(st, previous, now, "closed candle "+bar["dt"])
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
    previous = phase_snapshot(st)
    model.running_bar(st, symbol, current, now)
    record_transitions(st, previous, now, "running candle")


def alert_text(e):
    labels = dict(DIRECT_BOS_CONFIRMED="ORIGINAL SIGNAL ON BOS — CLOSE CONFIRMED",
                  RUNNING_BREAK="RUNNING BOS — unconfirmed",
                  CLOSE_CONFIRMED="CANDLE CLOSE — BOS CONFIRMED",
                  CLOSE_FAILED="CANDLE CLOSE — BOS FAILED (not an entry)",
                  CLOSE_ONLY="CANDLE CLOSE — BOS confirmed; running crossing was not observed")
    coincident = e.get("coincident_original_signals", [])
    extra = ("Coincident final labels: "+", ".join(x["signal"] for x in coincident)+"\n") if coincident else ""
    if e.get("sector_eligible_at"):
        extra += f"Sector eligibility verified {model.stamp(e['sector_eligible_at']).strftime('%H:%M:%S')} IST after board publication\n"
    gate = e.get("sector_gate", {})
    if gate.get("status") == "pass":
        extra += f"Sector {gate['sector']} · persistent {gate['score']:.2f} >70 · top 3 · board close {model.stamp(gate['close']).strftime('%H:%M')} IST\n"
    return (f"M16 · {labels.get(e['kind'], e['kind'])}\n{e['symbol']} {e['side']} @ {e['price']:.2f}\n"
            f"BOS {e['bos']:.2f} · pullback {e['pullback']}\n"
            f"Original {e['signal']} candle {model.stamp(e['seed']).strftime('%H:%M')}\n"
            f"Breakout candle {model.stamp(e['bar']).strftime('%H:%M')} · observed {model.stamp(e['observed']).strftime('%H:%M:%S')} IST\n"
            + extra + "Feed-polled observation, not a guaranteed tick-time fill. Alerts only; no live order.")


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


def sector_send_ok(st, event, now):
    if not st.get("sector_gate_required") or event["kind"] in {"CLOSE_FAILED", "CLOSE_UNAVAILABLE"}:
        return True
    # Never resurrect an old pre-gate outbox item or a failed detection.
    return (event.get("sector_gate", {}).get("status") == "pass" and
            m16_sector.decision(st, event["symbol"], event["side"], now)["status"] == "pass")


def flush(st, now, dry_run=False):
    """Durable per-event outbox. Uncertain crash sends are not blindly repeated."""
    batch_started = time.monotonic()
    batch_now = model.stamp(now)
    for item in st["outbox"].values():
        if item["status"] not in {"pending", "failed"}:
            continue
        now = batch_now + pd.Timedelta(seconds=time.monotonic()-batch_started)
        if item.get("retry_after") and model.stamp(now) < model.stamp(item["retry_after"]):
            continue
        e = item["event"]
        if not event_fresh(e, now):
            item["status"] = "expired_unsent"
            continue
        if item.get("awaiting_sector"):
            gate = m16_sector.decision(st, e["symbol"], e["side"], now)
            item["sector_last_check"] = gate
            if gate["status"] == "block":
                item["status"] = "sector_rejected"
                item["awaiting_sector"] = False
                continue
            if gate["status"] == "pass":
                e["sector_gate_at_detection"] = e["sector_gate"]
                e["sector_gate"] = gate
                e["sector_eligible_at"] = now.isoformat()
                e["live"] = True
                item["awaiting_sector"] = False
                # JSON persistence duplicates event objects; update both copies.
                for saved in st["events"]:
                    if saved["id"] == e["id"]:
                        saved.update(e)
        if not sector_send_ok(st, e, now):
            item["sector_blocked"] = True
            continue
        if dry_run:
            print("[DRY] " + alert_text(e))
            item["status"] = "dry_run"
            continue
        item["status"] = "failed"  # per-target ledger protects acknowledged sends
        ledger = item.setdefault("deliveries", {})
        # Recheck between bot sends too: a slow/failing first bot must not
        # make the mirror send an already-closed "running candle" alert.
        def can_send():
            current = batch_now + pd.Timedelta(seconds=time.monotonic()-batch_started)
            return event_fresh(e, current) and sector_send_ok(st, e, current)
        delivered = deliver_targets(st, ledger, lambda token, chat:
                                    tg.send_message_to(token, chat, html.escape(alert_text(e))),
                                    can_send=can_send)
        item["status"] = "sent" if delivered else "failed"
        if item["status"] == "failed":
            item["retry_after"] = (model.stamp(now) + pd.Timedelta(seconds=3)).isoformat()
        save(st)


def finish(st, dry_run=False):
    """Full-day TXT + PDF; independent per-format and per-bot retry ledgers."""
    if st.get("eod_done"):
        return
    if dry_run:
        print("[DRY] M16 TXT + PDF full-day review")
        return
    import m16_audit
    text = "\n\n".join(name+"\n"+"\n".join(lines) for name,lines in m16_audit.sections(st))
    report = STATE.parent / f"activity_M16_{st['date']}.txt"
    report.write_text(text, encoding="utf-8")
    txt_ok = deliver_targets(st, st.setdefault("eod_deliveries", {}), lambda token, chat: tg.send_document_to(
        token, chat, str(report), caption=f"M16 full-day review {st['date']} (TXT)"))
    pdf_ok = False
    try:
        pdf = m16_audit.build_pdf(st, STATE.parent / f"audit_M16_{st['date']}.pdf")
    except Exception as exc:
        st["diagnostics"].append(dict(reason=f"PDF build failed: {type(exc).__name__}: {exc}"))
        save(st)
        print("M16 PDF build failed; TXT delivery preserved, PDF can retry")
    else:
        pdf_ok = deliver_targets(st, st.setdefault("eod_pdf_deliveries", {}), lambda token, chat: tg.send_document_to(
            token, chat, str(pdf), caption=f"M16 detailed full-day audit {st['date']} (PDF)"))
    st["eod_done"] = txt_ok and pdf_ok
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
    expire_setups(st, now)
    trim_diagnostics(st)
    save(st, dry_run)


def record_feed_failure(st, symbol, now, reason):
    health = st.setdefault("feed_health", {})
    previous = health.get(symbol, {})
    count = previous.get("failure_count", 0) + 1
    first = previous.get("first_failure", now.isoformat()) if not previous.get("ok", True) else now.isoformat()
    health[symbol] = dict(ok=False, at=now.isoformat(), first_failure=first,
                          failure_count=count, reason=reason,
                          last_success=previous.get("last_success", previous.get("at") if previous.get("ok") else None))
    episodes = st.setdefault("feed_outages", {}).setdefault(symbol, [])
    if not episodes or episodes[-1].get("recovered_at"):
        episodes.append(dict(start=first, count=0))
    episodes[-1].update(last_failure=now.isoformat(), count=episodes[-1]["count"]+1, reason=reason)
    # Persist counts without flooding the audit and evicting outage evidence.
    if previous.get("ok", True) or previous.get("reason") != reason or count % 60 == 0:
        st["diagnostics"].append(dict(at=now.isoformat(), symbol=symbol, reason=reason))


def record_feed_success(st, symbol, now, metadata):
    episodes = st.get("feed_outages", {}).get(symbol, [])
    if episodes and not episodes[-1].get("recovered_at"):
        episodes[-1]["recovered_at"] = now.isoformat()
    st.setdefault("feed_health", {})[symbol] = dict(
        **metadata, at=now.isoformat(), last_success=now.isoformat(), ok=True)


def notify_health(st, now, dry_run=False):
    if m16_calendar.session_status(now)[0] == "closed":
        return
    # No completed 5m candle is due before 09:20. Allow the normal
    # publication grace before declaring initial discovery unhealthy.
    if model.stamp(now).strftime("%H:%M:%S") < "09:20:30":
        return
    health = st.get("feed_health", {})
    bad, details, watched_symbols = [], {}, []
    for sym in shared.L.SYMS:
        rec = health.get(sym, {})
        # Symbols no longer scheduled after discovery are not falsely called stale.
        discovery_missing = not st.get("cursors", {}).get(sym) or model.stamp(st["cursors"][sym]).strftime("%H:%M") < "10:50"
        watched = model.before(now, "11:00:00") or discovery_missing or any(
            x["symbol"] == sym for x in st["setups"].values())
        if watched:
            watched_symbols.append(sym)
        expected = (model.stamp(now)-pd.Timedelta(seconds=30)).floor("5min")-pd.Timedelta(minutes=5)
        latest = rec.get("latest")
        stale = (model.stamp(latest) < expected) if latest else (
            not rec.get("at") or (model.stamp(now)-model.stamp(rec["at"])).total_seconds() > 90)
        history_bad = st.get("history_health", {}).get(sym) is False
        cursor = st.get("cursors", {}).get(sym)
        analysis_bad = (sym in st.get("history_health", {}) and
                        expected.strftime("%H:%M") >= "09:15" and
                        (not cursor or model.stamp(cursor) < expected))
        if watched and (not rec.get("ok") or stale or history_bad or analysis_bad or
                        (not model.before(now, "11:00:00") and discovery_missing)):
            bad.append(sym)
            reasons = []
            if not rec.get("ok"): reasons.append(rec.get("reason", "no usable response"))
            elif stale: reasons.append("stale candle coverage")
            if history_bad: reasons.append("indicator history unavailable")
            if analysis_bad: reasons.append("analysis cursor behind expected close")
            if not model.before(now, "11:00:00") and discovery_missing: reasons.append("morning discovery incomplete")
            details[sym] = dict(reasons=reasons, latest=latest, cursor=cursor, expected=expected.isoformat())
    st["monitoring_health"] = dict(at=now.isoformat(), affected=bad, watched=watched_symbols, details=details)
    sector_unknown = st.get("sector_gate_required") and model.before(now, "12:35:00") and m16_sector.decision(st, "__HEALTH__", "BUY", now).get("reason") in {"sector board unavailable", "sector board stale"}
    st["monitoring_health"]["sector_gate_unavailable"] = bool(sector_unknown)
    status = "degraded" if bad or sector_unknown else "healthy"
    old = st.get("health_notice", {})
    # Session-scoped, persisted dedupe: never repeat operational notices,
    # even after recovery/degradation flapping or a workflow restart.
    notices = st.setdefault("health_notices", {})
    if old.get("status") and old.get("complete"):
        notices.setdefault(old["status"], old)
    notice = notices.setdefault(status, dict(complete=False))
    if notice.get("complete"):
        st["health_notice"] = dict(notice, status=status, affected=bad,
                                  checked_at=now.isoformat())
        return
    text = (f"M16 FEED {status.upper()}: {len(bad)} monitored symbols have feed/history/analysis gaps. "
            "Live alerts may be missed or delayed; stale entries will NOT be sent. "
            "Polling is not tick-time execution.") if bad else (
            "M16 feed monitoring healthy on latest poll; polling latency still applies.")
    if sector_unknown:
        text = "M16 sector board missing/stale: entry alerts blocked until current persistence data is available. " + text
    if bad:
        text += "\n" + "\n".join(
            f"{sym}: {', '.join(details[sym]['reasons'])[:180]}; latest={details[sym]['latest']}; cursor={details[sym]['cursor']}"
            for sym in bad[:3])
    # Preserve successful destinations when retrying a partial send.
    ledger = notice.setdefault("deliveries", old.get("deliveries", {})
                               if old.get("status") == status else {})
    complete = True if dry_run else deliver_targets(st, ledger,
        lambda token, chat: tg.send_message_to(token, chat, text))
    notice.update(status=status, at=now.isoformat(), affected=bad,
                  deliveries=ledger, complete=complete)
    st["health_notice"] = dict(notice)
    print(text)


def timed_fetch(sym):
    # Conservative snapshot watermark: a request started BEFORE a candle
    # closed cannot prove finality merely by returning AFTER the close.
    started = shared.now_ist()
    bars, health = m16_feed.fetch(sym, shared.L.SID.get(sym), started)
    health["received_at"] = shared.now_ist().isoformat()
    health["request_started"] = started.isoformat()
    bars.attrs["m16_feed"] = health
    return bars, started


def needs_discovery_refresh(cached, target):
    """A pre-close snapshot is not final merely because the loop clock advanced."""
    closed = shared._closed_bars(cached[0], cached[1]) if cached else None
    return closed is None or closed.empty or model.stamp(closed.iloc[-1]["dt"]) < target


def discovery_retries(symbols, latest, now):
    if not model.before(now, "11:00:00"):
        return set()
    target = (model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")-pd.Timedelta(minutes=5)
    return {sym for sym in symbols if needs_discovery_refresh(latest.get(sym), target)}


def running_snapshot_usable(bars, started, observed):
    receipt = model.stamp(bars.attrs.get("m16_feed", {}).get("received_at", started))
    return (0 <= (model.stamp(observed)-receipt).total_seconds() <= 5 and
            0 <= (receipt-model.stamp(started)).total_seconds() <= MAX_LAG)


def live_session(st, cache, max_minutes, dry_run=False):
    """Non-blocking discovery: monitoring never waits for a universe-wide batch.

    Separate active-analysis workers keep the morning seed-discovery queue from
    starving existing setups. Network requests still have upstream latency.
    """
    if m16_calendar.session_status(shared.now_ist())[0] == "closed":
        print("M16 market closed; no regular-session monitoring")
        return
    st["sector_gate_required"] = True
    sector_future, sector_retry = None, 0.0
    started, last_save = time.monotonic(), 0.0
    poll = max(1., float(os.getenv("M16_POLL_SECONDS", "1")))
    last_discovery, fetches, engines, latest, requested = None, {}, {}, {}, {}
    worker_count = max(1, int(os.getenv("M16_ENGINE_WORKERS", "4")))
    next_health, next_heartbeat = 0.0, 0.0
    outage_since = None
    (ROOT / ".m16-unhealthy").unlink(missing_ok=True) if not dry_run else None
    failures, retry_at = {}, {}
    with ProcessPoolExecutor(max_workers=1) as sector_pool, ThreadPoolExecutor(max_workers=16) as fetch_pool, ThreadPoolExecutor(max_workers=8) as watch_fetch, \
            ProcessPoolExecutor(max_workers=worker_count) as discovery, ProcessPoolExecutor(max_workers=2) as active_pool:
        warm_future = sector_pool.submit(m16_sector.warm, st["date"])
        while time.monotonic()-started < max_minutes*60:
            now = shared.now_ist()
            if warm_future is not None and warm_future.done():
                try:
                    counts = warm_future.result()
                    st["sector_warmup"] = dict(at=now.isoformat(), counts=counts)
                except Exception as exc:
                    st["sector_warmup"] = dict(at=now.isoformat(), error=str(exc))
                warm_future = None
            if now.strftime("%Y-%m-%d") != st["date"]:
                break
            if not model.before(now, "15:31:00"):
                expire_setups(st, now)
                finish(st, dry_run)
                break
            if model.before(now, "09:15:00"):
                time.sleep(.5)
                continue
            if sector_future is not None and sector_future.done():
                try:
                    snapshot = sector_future.result()
                    st.setdefault("sector_snapshots", []).append(snapshot)
                    st["sector_health"] = dict(ok=True, at=now.isoformat())
                except Exception as exc:
                    st["sector_health"] = dict(ok=False, reason=str(exc), at=now.isoformat())
                sector_future = None
            current_board = st.get("sector_snapshots", [])
            ready = current_board and model.stamp(current_board[-1]["close"]) == model.stamp(now).floor("5min")
            if sector_future is None and not ready and time.monotonic() >= sector_retry and model.before(now, "12:35:00"):
                sector_future = sector_pool.submit(m16_sector.refresh, now)
                sector_retry = time.monotonic()+5
            active = {s["symbol"] for s in st["setups"].values() if s["phase"] not in model.TERMINAL}
            bucket = (model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")
            scan = last_discovery != bucket and model.before(now, "11:00:00")
            symbols = set(shared.L.SYMS) if scan else set()
            if scan:
                last_discovery = bucket
            # Re-fetch missing/late discovery bars during the freshness budget.
            # A batch arriving with yesterday's/previous candle's data must not
            # silently lose a morning signal until the next full discovery.
            symbols.update(discovery_retries(shared.L.SYMS, latest, now))
            # Terminal/expired seeds stay under slower observation for the
            # full-day research audit, without creating new entries.
            audit_symbols = {s["symbol"] for s in st["setups"].values()}
            symbols |= active | audit_symbols
            # Recover missed morning discovery even after the seed cutoff.
            # apply/model still enforce original candle cutoffs and no backdating.
            for sym in shared.L.SYMS:
                cursor = st["cursors"].get(sym)
                if not model.before(now, "11:00:00") and (not cursor or model.stamp(cursor).strftime("%H:%M") < "10:50"):
                    symbols.add(sym)
            busy_fetch = set(fetches.values())
            for sym in sorted(symbols, key=lambda s: (s not in active, s)):
                interval = poll if sym in active else (30. if sym in audit_symbols or not model.before(now, "11:00:00") else max(5., poll))
                if sym not in busy_fetch and time.monotonic() >= retry_at.get(sym, 0) and time.monotonic()-requested.get(sym, -1e10) >= interval:
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
                        failures.pop(sym, None)
                        retry_at.pop(sym, None)
                        record_feed_success(st, sym, now, raw.attrs.get("m16_feed", {}))
                    else:
                        raise ValueError("current-session feed unavailable")
                except Exception as exc:
                    record_feed_failure(st, sym, now, str(exc))
                    failures[sym] = failures.get(sym, 0)+1
                    retry_at[sym] = time.monotonic()+min(30, 2**min(failures[sym], 5))
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
                if running_snapshot_usable(bars, received, observed):
                    observe_running(st, sym, bars, observed)
                # Completion is judged at REQUEST START, not the later loop
                # clock. A cached forming candle must never become a final
                # candle merely because time passed without a fresh response.
                closed = shared._closed_bars(bars, received)
                if closed is None or closed.empty or sym in busy_engine:
                    continue
                cursor = st["cursors"].get(sym)
                if cursor and model.stamp(closed.iloc[-1]["dt"]) <= model.stamp(cursor):
                    continue
                if not cache.get(sym, {}).get("ok"):
                    continue
                executor = active_pool if sym in active else discovery
                engines[executor.submit(analyze, (sym, bars, cache[sym], cursor, received))] = sym
            flush(st, shared.now_ist(), dry_run)
            if time.monotonic() >= next_health:
                notify_health(st, now, dry_run)
                monitored = st.get("monitoring_health", {})
                watched = monitored.get("watched", [])
                bad = monitored.get("affected", [])
                widespread = len(watched) >= 3 and len(bad)/len(watched) >= .8
                clock = time.monotonic()
                outage_since = (clock if outage_since is None else outage_since) if widespread else None
                if outage_since is not None and clock-outage_since >= float(os.getenv("M16_UNHEALTHY_EXIT_SECONDS", "300")):
                    save(st, dry_run)
                    if not dry_run:
                        (ROOT / ".m16-unhealthy").write_text(now.isoformat())
                    for pending in list(engines)+list(fetches):
                        pending.cancel()
                    raise SystemExit("M16 sustained widespread outage; requesting supervised restart")
                next_health = time.monotonic() + 30
            if time.monotonic() >= next_heartbeat:
                print("M16 progress " + json.dumps(dict(at=now.isoformat(),
                    processed_symbols=len(st["cursors"]), setups=len(st["setups"]),
                    events=len(st["events"]), pending_fetches=len(fetches), pending_engines=len(engines),
                    unhealthy=len(st.get("monitoring_health", {}).get("affected", [])))))
                next_heartbeat = time.monotonic()+60
            expire_setups(st, shared.now_ist())
            trim_diagnostics(st)
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
    st["mode"] = "offline_replay"
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
    expire_setups(st, end)
    return st


def feed_probe(symbols=None):
    """Read-only Yahoo probe: no state writes, bot messages or trade alerts."""
    report = []
    for sym in list(symbols or shared.L.SYMS[:3]):
        try:
            bars, health = m16_feed.fetch(sym, shared.L.SID.get(sym), shared.now_ist())
            report.append(dict(symbol=sym, ok=True, health=health))
        except Exception as exc:
            report.append(dict(symbol=sym, ok=False, reason=str(exc)))
    print(json.dumps(report, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--feed-probe", action="store_true")
    parser.add_argument("--max-runtime-min", type=float, default=324)
    parser.add_argument("--replay-date", help="Offline YYYY-MM-DD disk replay; no bot or feed calls")
    parser.add_argument("--symbols", nargs="+", default=None, help="Optional replay symbol subset")
    args = parser.parse_args()
    if args.replay_date:
        result = replay(args.replay_date, args.symbols or list(shared.L.SYMS))
        print(pformat(result, width=120))
        return
    if args.feed_probe:
        feed_probe(args.symbols)
        return
    if args.test_alert:
        if not test_alert(args.dry_run):
            raise SystemExit("M16 test alert failed for one or more destinations")
        return
    now = shared.now_ist()
    status, reason = m16_calendar.session_status(now)
    if status == "closed":
        print(f"M16 market closed: {reason}; no feed requests, entries or degraded notices")
        return
    if status == "unknown":
        print(f"M16 calendar warning: {reason}")
    if now.weekday() >= 5 or now.strftime("%H:%M") < "09:00" or now.strftime("%H:%M") >= "15:40":
        print("M16 outside weekday 09:00–15:40 IST window; no replay alerts")
        return
    if not args.dry_run:
        credentials()
    st = load(now.strftime("%Y-%m-%d"))
    if st.get("eod_done"):
        return
    st["sector_gate_required"] = True
    cache = {sym: shared.symbol_history(sym, st["date"]) for sym in shared.L.SYMS}
    st["history_health"] = {sym: bool(rec.get("ok")) for sym, rec in cache.items()}
    missing_history = [sym for sym, rec in cache.items() if not rec.get("ok")]
    if missing_history:
        st["diagnostics"].append(dict(at=now.isoformat(), reason="history unavailable", symbols=missing_history))
        print(f"M16 history unavailable for {len(missing_history)} symbols; recorded in diagnostics")
    if args.session:
        limit = max(1, int(os.getenv("M16_MAX_SESSION_STARTS", "12")))
        if st.get("session_starts", 0) >= limit:
            raise SystemExit("M16 supervised restart cap reached")
        st["session_starts"] = st.get("session_starts", 0)+1
        save(st, args.dry_run)
        live_session(st, cache, args.max_runtime_min, args.dry_run)
    else:
        try:
            st.setdefault("sector_snapshots", []).append(m16_sector.refresh(shared.now_ist()))
        except Exception as exc:
            st["sector_health"] = dict(ok=False, reason=str(exc))
        bars_map = {}
        chosen = list(shared.L.SYMS[:max(1, int(os.getenv("M16_DRY_RUN_SYMBOLS", "3")))]) if args.dry_run else list(shared.L.SYMS)
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = {pool.submit(timed_fetch, sym): sym for sym in chosen}
            for future in as_completed(pending):
                sym = pending[future]
                try:
                    bars_map[sym], _ = future.result()
                except Exception as exc:
                    record_feed_failure(st, sym, shared.now_ist(), str(exc))
        cycle(st, now, bars_map, cache, dry_run=args.dry_run)
        if not model.before(shared.now_ist(), "15:31:00"):
            finish(st, args.dry_run)


if __name__ == "__main__":
    main()
