#!/usr/bin/env python3
"""Independent M16 continuation alerts. Shared M6 feed; dedicated bot/state.

Best-effort polling is NOT a tick feed. Runtime and upstream publication delay
are reported, never disguised as an exact one-second execution guarantee.
"""
from __future__ import annotations
import argparse
import json
import multiprocessing
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
# Discovery rotation: the universe is fetched in this many rotating slices so a
# single bucket no longer fires one full-universe wave at the provider.
DEFAULT_DISCOVERY_SLICES = 3


def sector_mode():
    return model.sector_mode()


def sector_advisory():
    return model.advisory()


def suppress_duplicates():
    return model.suppress_duplicates()


def signal_tier(name):
    return model.signal_tier(name)


ENTRY_KINDS = model.ENTRY_KINDS


EXTRA_UNIVERSE = ROOT / "m16_extra_universe.csv"


def extra_universe():
    """Optional stock universe beyond the F&O map (NSE names with no F&O contract).

    `m16_extra_universe.csv` lists the major liquid NSE names that have no F&O
    contract, so M16 could never see them at all (defence names such as GRSE or
    PARAS were the first case). They are added to the SCANNED universe only: the
    sector baskets and the M6 board stay exactly as they are, so no sector score
    changes because of this file.
    """
    if str(os.getenv("M16_EXTRA_UNIVERSE", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return []
    if not EXTRA_UNIVERSE.exists():
        return []
    rows, seen = [], set()
    for line in EXTRA_UNIVERSE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.lower().startswith("symbol,"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        symbol, sector = parts[0].upper(), parts[1].upper()
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append((symbol, sector))
    return rows


def install_extra_universe():
    """Extend this process's scan list; never the sector baskets."""
    added = []
    for symbol, sector in extra_universe():
        if symbol not in shared.SECTOR_OF:
            shared.SECTOR_OF[symbol] = sector
        if symbol not in shared.L.SYMS:
            shared.L.SYMS.append(symbol)
            added.append(f"{symbol}({sector})")
    if added:
        sectors = sorted({sector for _, sector in extra_universe()})
        print(f"M16 extra universe: {len(added)} symbol(s) added to scanning only across "
              f"{len(sectors)} sector labels ({', '.join(sectors)})")
    return added


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
        tmp.write_text(json.dumps(st, separators=(",", ":"), ensure_ascii=False, allow_nan=False), encoding="utf-8")
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


_WORKER_HISTORY: dict[tuple[str, str], dict] = {}


def _resolve_worker_cache(symbol, cache):
    if not isinstance(cache, dict) or not cache.get("ok"):
        return cache or {}
    if "eng_hist" in cache or not cache.get("day"):
        return cache
    key = (symbol, str(cache["day"]))
    hit = _WORKER_HISTORY.get(key)
    if hit is None:
        hit = shared.symbol_history(symbol, str(cache["day"]))
        _WORKER_HISTORY[key] = hit
    return hit


def inactive_symbols(cache, stale_days=7):
    """Symbols whose local history last_day trails peers (delisted/dormant on Yahoo).

    An extra-universe name with no history file at all is dormant too: it cannot
    produce a seed (analyze() needs a valid prior-day cache), so polling it would
    only spend the shared provider budget. It joins the rotation by itself once
    the next bootstrap writes its history.
    """
    dormant = set(m16_sector.dormant_members(cache, stale_days=stale_days))
    dormant |= {sym for sym, _ in extra_universe() if not (cache.get(sym) or {}).get("ok")}
    return dormant


def analyze(job):
    """Independent worker: causal indicator evaluation, every EX switch enabled.

    Only the current prefix's last row can create a seed. In particular a
    full-day engine run is never used to infer historical labels.
    """
    symbol, bars, cache, cursor, now = job
    cache = _resolve_worker_cache(symbol, cache)
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
                raw_code = int(res.iloc[-1]["scan_code"])
                if raw_code != 0:
                    code = raw_code
                    name = str(res.iloc[-1].get("scan_name", raw_code))
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
        # A board that published after this candle closed is M16's own delay,
        # not the signal's: credit it so the observation is not mislabelled.
        credit = board_publication_credit(st, bar)
        model.closed_bar(st, symbol, bar, now, MAX_LAG + credit)
        if credit:
            for produced in st["events"]:
                if (produced.get("symbol") == symbol and not produced.get("board_wait")
                        and produced.get("bar") == model.stamp(bar["dt"]).isoformat()):
                    produced["board_wait"] = credit
                    if produced["id"] in st["outbox"]:
                        st["outbox"][produced["id"]]["event"] = produced
        if code is not None:
            key = model.seed(st, symbol, code, name, bar, pd.DataFrame(context))
            if key:
                model.direct_original_bos(st, key, bar, pd.DataFrame(context), now, MAX_LAG + credit)
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
    queue_late_entries(st, symbol, rows, now)


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


def alert_text(e, markup=False):
    """One alert message. `markup=True` bolds the symbol and links it to its chart.

    Everything else is escaped, so symbols such as M&M or GVT&D cannot break
    Telegram's HTML parser.
    """
    labels = dict(DIRECT_BOS_CONFIRMED="ORIGINAL SIGNAL ON BOS — CLOSE CONFIRMED",
                  RUNNING_BREAK="RUNNING BOS — unconfirmed",
                  CLOSE_CONFIRMED="CANDLE CLOSE — BOS CONFIRMED",
                  CLOSE_FAILED="CANDLE CLOSE — BOS FAILED (not an entry)",
                  CLOSE_ONLY="CANDLE CLOSE — BOS confirmed; running crossing was not observed")
    coincident = e.get("coincident_original_signals", [])
    extra = "".join(f"Coincident final labels: {x['signal']}\n" for x in coincident[:1])
    ext = entry_extension_pct(e)
    entry_line = f"Entry {ext:+.2f}% beyond BOS" if ext is not None else "Entry level unavailable"
    if ext is not None and ext > 0.2:
        entry_line += " · EXTENDED — a retest of BOS is the better entry"
    if model.stamp(e["bar"]).strftime("%H:%M") < "10:15":
        entry_line += " · early window (wider noise)"
    gate = e.get("sector_gate_at_send") or e.get("sector_gate") or {}
    sector_line, risk_line = sector_context_lines(gate)
    if e.get("ema_undercut"):
        risk_line = ((risk_line + "\n") if risk_line else "") + (
            f"Pullback undercut EMA9/10 by {float(e['ema_undercut']):.2f}% "
            f"(tolerance {model.ema_tolerance_pct():.2f}%) — watched, not invalidated.")
    late = e.get("late") or {}
    if late:
        extra += (f"Delayed discovery: breakout candle closed {late['delay_minutes']:.1f} min before "
                  f"detection; price {late['extension_pct']:.2f}% beyond BOS "
                  f"(breakout candle close {late['original_price']:.2f}). Entry level is the discovery "
                  "price, not the BOS close.\n")
        header = (f"M16 · DELAYED — {labels.get(e['kind'], e['kind'])} "
                  f"(found {late['delay_minutes']:.1f} min late)")
    else:
        header = f"M16 · {labels.get(e['kind'], e['kind'])}"
    text = (f"{header}\n"
            f"{e['symbol']} · {e['side']} @ {e['price']:.2f}\n"
            f"BOS {e['bos']:.2f} · pullback {e['pullback']} · {e['signal']} "
            f"(seed {model.stamp(e['seed']).strftime('%H:%M')}) · tier {signal_tier(e['signal'])}\n"
            f"{entry_line}\n"
            + (sector_line + "\n" if sector_line else "")
            + (risk_line + "\n" if risk_line else "")
            + extra
            + f"Breakout candle {model.stamp(e['bar']).strftime('%H:%M')} · "
              f"observed {model.stamp(e['observed']).strftime('%H:%M:%S')} IST\n"
              "Feed-polled observation, not a guaranteed tick-time fill. Alerts only; no live order.")
    if not markup:
        return text
    escaped = tg.esc(text)
    symbol = tg.esc(str(e["symbol"]))
    # The bold symbol is a chart deep link: one tap opens the stock's chart on
    # Dhan's TradingCharts. F&O names carry their Dhan security id; the extras
    # only have their NSE ticker, which is TradingCharts' published instrument.
    return escaped.replace(f"{symbol} · {e['side']} @",
                           f"{tg.chart_link(e['symbol'], security_id(e['symbol']))} · {e['side']} @", 1)


def security_id(symbol):
    """Dhan security id for `symbol`, or None when the universe file has none."""
    try:
        sid = shared.L.SID.get(symbol)
    except Exception:
        return None
    if sid is None:
        return None
    sid = str(sid).strip()
    return int(float(sid)) if sid not in {"", "nan", "None"} else None


def sector_context_lines(gate):
    """Two lines of sector context; never a claim of a fresh board we do not have."""
    status = (gate or {}).get("status")
    if status == "disabled":
        return "", ""
    sector, score = gate.get("sector"), gate.get("score")
    if not sector or score is None:
        if status == "unknown":
            reason = gate.get("reason") or "sector board not available"
            if sector and score is None:
                # Grouped but not a board sector (TEXTILE, SUGAR, SHIPPING): say
                # which one, so the line is a fact and not a shrug.
                reason = f"{sector} is grouped but has no board ({reason})"
            return f"Sector context unavailable ({reason}) — sector unverified", ""
        return "", ""
    slope, rank, breadth = gate.get("slope15"), gate.get("known_rank"), gate.get("boards_above70")
    bits = [f"Sector {sector} {float(score):.2f}"]
    if slope is not None:
        bits.append(f"15m {float(slope):+.1f} {'↓' if slope < -0.5 else ('↑' if slope > 0.5 else '→')}")
    if rank:
        bits.append(f"rank {rank}")
    if breadth is not None:
        bits.append(f"{breadth} sectors above 70")
    if status == "unknown":
        bits.append("board not published — sector unverified")
    elif status == "block":
        bits.append("below the old gate — advisory only")
    line = " · ".join(bits)
    risk = ""
    if slope is not None and slope <= -1:
        risk = ("⚠ Sector score is falling — the sector move is already behind it; "
                "late-cycle, not a fresh rotation.")
    elif slope is not None and slope >= 1 and breadth is not None and breadth <= 3:
        risk = "Sector score rising on a narrow board — the early-rotation shape."
    elif breadth is not None and breadth >= 4 and slope is not None and slope < 0.5:
        risk = "⚠ Broad board and a flat sector — crowded; expect chop."
    return line, risk


def entry_extension_pct(event):
    """How far beyond the frozen BOS the alert price already is, in trade direction."""
    try:
        sign = 1 if event["side"] == "BUY" else -1
        return sign * (float(event["price"]) - float(event["bos"])) / float(event["bos"]) * 100.
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return None


def board_publication_credit(st, bar):
    """Credit for the board's own publication lag, from a raw candle dict."""
    if not st.get("sector_gate_required") or sector_advisory():
        return 0.
    observed = (st.get("sector_snapshots") or [{}])[-1].get("observed")
    if not observed:
        return 0.
    end = model.stamp(bar["dt"]) + pd.Timedelta(minutes=5)
    cap = max(0., float(os.getenv("M16_BOARD_WAIT_SECONDS", "60")))
    return float(max(0., min(cap, (model.stamp(observed)-end).total_seconds())))


def board_wait_seconds(st, event):
    """How much of a close alert's window was spent waiting for the sector board.

    The 30s window measures how stale an observation is, not how long M16 spent
    waiting for its own gate. A board that publishes 20-40s after the candle
    close (the normal publication lag) pushed confirmations past 30s and made
    them look "found late" when nothing had been missed. The wait is credited
    back below, capped, and only for the gate the alert actually needed.
    """
    if event.get("kind") not in {"CLOSE_CONFIRMED", "CLOSE_ONLY", "DIRECT_BOS_CONFIRMED"}:
        return 0.
    return board_publication_credit(st, dict(dt=event["bar"]))


def price_at_level(event, price):
    """Trade side of the frozen BOS, within the delayed-alert extension limit."""
    try:
        sign = 1 if event["side"] == "BUY" else -1
        extension = sign * (float(price) - float(event["bos"])) / float(event["bos"]) * 100.
    except (TypeError, ValueError, ZeroDivisionError, KeyError):
        return False
    return 0 < extension <= late_alert_max_extension_pct()


def event_fresh(event, now, grace=0.):
    now = model.stamp(now)
    start = model.stamp(event["bar"])
    age = (now-model.stamp(event["observed"])).total_seconds()
    # Time spent waiting for our own gate is not staleness of the observation.
    allowance = grace + float(event.get("board_wait", 0.))
    if now.strftime("%Y-%m-%d") != start.strftime("%Y-%m-%d") or not 0 <= age <= MAX_LAG + allowance:
        return False
    # A delayed re-entry is fresh from the moment we discovered it; the staleness
    # of the BOS candle itself is bounded by late_entry_candidate, and the send
    # still has to happen inside the same 30s observation budget.
    if event.get("late"):
        return event["kind"] != "RUNNING_BREAK" and model.before(now, "12:30:00")
    end = start+pd.Timedelta(minutes=5)
    if event["kind"] == "RUNNING_BREAK":
        return start <= now < end  # NEVER send a provisional alert after close
    # A gate that published late extends only its own alert's window.
    return 0 <= (now-end).total_seconds() <= MAX_LAG + allowance


def sector_send_ok(st, event, now):
    if sector_advisory():
        # Advisory: the board is context in the message, never a reason to drop
        # a fresh signal after it was accepted for delivery.
        return True
    if not st.get("sector_gate_required") or event["kind"] in {"CLOSE_FAILED", "CLOSE_UNAVAILABLE"}:
        return True
    # Never resurrect an old pre-gate outbox item or a failed detection.
    return (event.get("sector_gate", {}).get("status") == "pass" and
            m16_sector.decision(st, event["symbol"], event["side"], now)["status"] == "pass")


def flush(st, now, dry_run=False):
    """Durable per-event outbox. Uncertain crash sends are not blindly repeated."""
    batch_started = time.monotonic()
    batch_now = model.stamp(now)
    promote_stranded_duplicates(st)
    for item in st["outbox"].values():
        if item["status"] not in {"pending", "failed"}:
            continue
        now = batch_now + pd.Timedelta(seconds=time.monotonic()-batch_started)
        if item.get("retry_after") and model.stamp(now) < model.stamp(item["retry_after"]):
            continue
        e = item["event"]
        # While the gate for this very candle is still unpublished, waiting is
        # legitimate up to the board cap; the promotion below pins the real wait.
        grace = max(0., float(os.getenv("M16_BOARD_WAIT_SECONDS", "60"))) if (
            item.get("awaiting_sector") and not sector_advisory()) else 0.
        if not event_fresh(e, now, grace):
            item["status"] = "expired_unsent"
            continue
        if item.get("awaiting_sector") and sector_advisory():
            # A state file written by gate mode (or a board that arrived late)
            # is context now: refresh it for the message and send.
            item["awaiting_sector"] = False
            e["sector_gate_at_send"] = m16_sector.decision(st, e["symbol"], e["side"], now)
            for saved in st["events"]:
                if saved["id"] == e["id"]:
                    saved.update(e)
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
                e["board_wait"] = board_wait_seconds(st, e)
                item["awaiting_sector"] = False
                # The waiting grace above kept the item alive; now that the real
                # board timestamps are known, the true window applies. A close
                # whose board arrived past the credited wait expires unsent.
                if not event_fresh(e, now):
                    item["status"] = "expired_unsent"
                    continue
                e["live"] = True
                # JSON persistence duplicates event objects; update both copies.
                for saved in st["events"]:
                    if saved["id"] == e["id"]:
                        saved.update(e)
        end = model.stamp(e["bar"]) + pd.Timedelta(minutes=5)
        late_but_gated = (e.get("board_wait") and not sector_advisory() and
                          (model.stamp(now)-end).total_seconds() > MAX_LAG)
        if late_but_gated and not price_at_level(e, e["price"]):
            # The wait was ours, but the level is gone: never dress a stale
            # price up as a live entry.
            item["status"] = "expired_unsent"
            item["awaiting_sector"] = False
            st.setdefault("diagnostics", []).append(dict(
                at=model.stamp(now).isoformat(), symbol=e["symbol"],
                reason=f"board published {e['board_wait']:.0f}s after the close and the price left the BOS"))
            continue
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
                                    tg.send_message_to(token, chat, alert_text(e, markup=True)),
                                    can_send=can_send)
        item["status"] = "sent" if delivered else "failed"
        if item["status"] == "failed":
            item["retry_after"] = (model.stamp(now) + pd.Timedelta(seconds=3)).isoformat()
        save(st)
    promote_stranded_duplicates(st)


def promote_stranded_duplicates(st):
    """A suppressed duplicate is only safe while its twin is really going out."""
    if not model.suppress_duplicates():
        return
    for item in st["outbox"].values():
        if item.get("status") != "duplicate_suppressed":
            continue
        twin = st["outbox"].get(item.get("suppresses"))
        if twin is None or twin.get("status") in {"failed", "expired_unsent", "sector_rejected"}:
            item["status"] = "pending"
            item["promoted_from_duplicate"] = twin.get("status") if twin else "missing"
            st.setdefault("diagnostics", []).append(dict(
                at=model.stamp(item["event"].get("observed") or "").isoformat() if item["event"].get("observed") else None,
                symbol=item["event"].get("symbol"),
                reason=f"duplicate of a twin that never went out ({item['promoted_from_duplicate']}); re-queued"))


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


def provider_throttled(reason):
    """True when a failure is provider-wide rate limiting, not symbol data.

    A 429/cooldown is global: retrying the other 200+ symbols cannot fix it and
    only deepens the cooldown. Symbol-level gaps (missing candle, empty body,
    stale publication) stay per-symbol.
    """
    text = str(reason or "").lower()
    return any(token in text for token in ("429", "cooldown", "rate_limited", "too many requests"))


def late_alert_window_seconds():
    """How stale a BOS close may be and still count as a tradable late entry."""
    return max(0., float(os.getenv("M16_LATE_WINDOW_MIN", "15"))) * 60.


def late_alert_max_extension_pct():
    """Maximum distance beyond the frozen BOS that still counts as 'at the level'."""
    return max(0., float(os.getenv("M16_LATE_MAX_EXTENSION_PCT", "0.75")))


def late_entry_candidate(st, event, now, discovery_price):
    """Price an alert at the discovery candle for a BOS found minutes late.

    The 30s freshness rule exists so a stale alert is never dressed up as a
    fresh one. It also threw away 2026-11-26's best signals: 8 of the 14
    backfilled ones were only 1.3-2.5 minutes late with price still at the
    frozen BOS (PRESTIGE closed +6.31%, HCLTECH +3.64%, MCX +3.51% from the
    discovery price). A delayed alert is allowed only when all of the
    following hold, and it is always labelled as delayed:
      * close-confirmed kind (never provisional RUNNING_BREAK, never CLOSE_FAILED)
      * 30s < delay <= M16_LATE_WINDOW_MIN (default 15 min)
      * still before the 12:30 confirmation cutoff
      * price on the trade side of the BOS and at most
        M16_LATE_MAX_EXTENSION_PCT (default 0.75 %) away from it
      * in gate mode only: the sector board validates the sector at that moment
    """
    if st.get("mode") == "offline_replay" or os.getenv("M16_LATE_ALERTS", "1") == "0":
        return None
    if event.get("kind") not in {"CLOSE_CONFIRMED", "CLOSE_ONLY", "DIRECT_BOS_CONFIRMED"}:
        return None
    if event.get("live") or event.get("late") or not event.get("bos") or not discovery_price:
        return None
    close_time = model.stamp(event["bar"]) + pd.Timedelta(minutes=5)
    delay = (model.stamp(now) - close_time).total_seconds()
    if delay <= MAX_LAG or delay > late_alert_window_seconds():
        return None
    if not model.before(now, "12:30:00"):
        return None
    sign = 1 if event["side"] == "BUY" else -1
    try:
        extension = sign * (float(discovery_price) - float(event["bos"])) / float(event["bos"]) * 100.
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if extension <= 0 or extension > late_alert_max_extension_pct():
        return None
    gate = m16_sector.decision(st, event["symbol"], event["side"], now)
    if not sector_advisory() and gate.get("status") not in {"pass", "disabled"}:
        return None
    return dict(delay_seconds=round(delay, 1), delay_minutes=round(delay / 60., 2),
                extension_pct=round(extension, 3), discovery_price=float(discovery_price),
                original_price=float(event["price"]), gate=gate)


def queue_late_entries(st, symbol, rows, now):
    """Queue a clearly-labelled delayed alert for a BOS discovered late."""
    if not rows:
        return
    try:
        discovery_price = float(rows[-1][0]["close"])
    except (TypeError, KeyError, IndexError):
        return
    for event in st["events"]:
        if event.get("symbol") != symbol:
            continue
        existing = st["outbox"].get(event["id"])
        # An event that waited for its sector board and then expired still gets
        # the delayed path: that is the safety net the live window exists for.
        if existing and existing.get("status") != "expired_unsent":
            continue
        info = late_entry_candidate(st, event, now, discovery_price)
        if not info:
            continue
        gate = info.pop("gate")
        event.update(late=info, sector_gate=gate,
                     sector_eligible_at=now.isoformat() if gate.get("status") == "pass" else None,
                     price=info["discovery_price"])
        st["outbox"][event["id"]] = dict(event=event, status="pending")
        print(f"M16 delayed alert queued: {symbol} {event['side']} {event['kind']} "
              f"({info['delay_minutes']:.1f} min late, {info['extension_pct']:.2f}% beyond BOS)")


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


def notify_health(st, now, dry_run=False, due=None):
    """Report real gaps, not the rotation's own schedule.

    `due` is the universe slice this bucket is scheduled to fetch. Symbols
    outside it are legitimately up to one rotation window old and must not be
    reported as feed outages; symbols inside it keep the strict freshness rule.
    """
    if m16_calendar.session_status(now)[0] == "closed":
        return
    # No completed 5m candle is due before 09:20. Allow the normal
    # publication grace before declaring initial discovery unhealthy.
    if model.stamp(now).strftime("%H:%M:%S") < "09:20:30":
        return
    health = st.get("feed_health", {})
    dormant = set(st.get("inactive_symbols", []))
    # A symbol is revisited every `slices` buckets, so one full rotation is the
    # legitimate age of bars outside the current slice (not slices-1 buckets).
    rotation = pd.Timedelta(minutes=5 * max(
        1, int(os.getenv("M16_DISCOVERY_SLICES", str(DEFAULT_DISCOVERY_SLICES)))))
    # A gap in an extra-universe name is not a degraded feed: those names are
    # scanned in addition to the core universe, so one thin or delisted ticker
    # must never raise an operational notice about the alert pipeline.
    extras = {sym for sym, _ in extra_universe()}
    bad, details, watched_symbols = [], {}, []
    for sym in shared.L.SYMS:
        if sym in dormant or sym in extras:
            continue
        rec = health.get(sym, {})
        # Symbols no longer scheduled after discovery are not falsely called stale.
        discovery_missing = not st.get("cursors", {}).get(sym) or model.stamp(st["cursors"][sym]).strftime("%H:%M") < "10:50"
        watched = model.before(now, "11:00:00") or discovery_missing or any(
            x["symbol"] == sym for x in st["setups"].values())
        if watched:
            watched_symbols.append(sym)
        expected = m16_feed.required_closed_bar(now)
        latest = rec.get("latest")
        pending = bool(due is not None and sym not in due)
        lag = rotation if pending else pd.Timedelta(0)
        stale = (model.stamp(latest) + lag < expected) if latest else (
            (not rec.get("at") and not pending) or bool(rec.get("at") and
            (model.stamp(now)-model.stamp(rec["at"])).total_seconds() > 90 + lag.total_seconds()))
        history_bad = st.get("history_health", {}).get(sym) is False
        cursor = st.get("cursors", {}).get(sym)
        analysis_bad = (sym in st.get("history_health", {}) and
                        expected.strftime("%H:%M") >= "09:15" and
                        ((not cursor and not pending) or
                         (bool(cursor) and model.stamp(cursor) + lag < expected)))
        # A symbol outside this bucket's slice is not "failing" merely because
        # its last attempt predates its turn; only an explicit failure older
        # than one rotation is reported while it waits.
        failed = rec.get("ok") is False
        if failed and pending and rec.get("at"):
            failed = (model.stamp(now)-model.stamp(rec["at"])).total_seconds() > rotation.total_seconds()
        if watched and (failed or stale or history_bad or analysis_bad or
                        (not model.before(now, "11:00:00") and discovery_missing)):
            bad.append(sym)
            reasons = []
            if failed: reasons.append(rec.get("reason", "no usable response"))
            elif stale: reasons.append("stale candle coverage")
            if history_bad: reasons.append("indicator history unavailable")
            if analysis_bad: reasons.append("analysis cursor behind expected close")
            if not model.before(now, "11:00:00") and discovery_missing: reasons.append("morning discovery incomplete")
            details[sym] = dict(reasons=reasons, latest=latest, cursor=cursor, expected=expected.isoformat())
    st["monitoring_health"] = dict(at=now.isoformat(), affected=bad, watched=watched_symbols, details=details)
    sector_reason = (m16_sector.decision(st, "__HEALTH__", "BUY", now).get("reason")
                     if st.get("sector_gate_required") and model.before(now, "12:35:00") else None)
    sector_unknown = sector_reason in {"sector board unavailable", "sector board stale"}
    st["monitoring_health"]["sector_gate_unavailable"] = bool(sector_unknown)
    partial_sectors = (st.get("sector_snapshots") or [{}])[-1].get("missing_sectors", []) if st.get("sector_gate_required") and model.before(now, "12:35:00") else []
    st["monitoring_health"]["missing_sectors"] = partial_sectors
    throttle = st.get("provider_throttle") or {}
    st["monitoring_health"]["provider_throttled"] = bool(
        throttle.get("at") and (model.stamp(now)-model.stamp(throttle["at"])).total_seconds() <= 300)
    status = "degraded" if bad or sector_unknown or partial_sectors else "healthy"
    old = st.get("health_notice", {})
    # Session-scoped, persisted dedupe: never repeat operational notices,
    # even after recovery/degradation flapping or a workflow restart.
    notices = st.setdefault("health_notices", {})
    if old.get("status") and old.get("complete"):
        notices.setdefault(old["status"], old)
    key, recovery = status, False
    if status == "healthy" and notices.get("healthy", {}).get("complete"):
        # The healthy state was already announced once. If the last *delivered*
        # notice was a degradation, entry alerts were reported as blocked and
        # silence would otherwise leave the operator unable to tell whether
        # they resumed. Announce that recovery, bounded so flapping is quiet.
        cap = max(0, int(os.getenv("M16_MAX_RECOVERIES", "2")))
        quiet = max(0., float(os.getenv("M16_RECOVERY_QUIET_SECONDS", "120")))
        delivered = int(st.get("health_recoveries", 0))
        age = (model.stamp(now)-model.stamp(old["at"])).total_seconds() if old.get("at") else 0
        # A recovery whose delivery is still unconfirmed retries immediately;
        # the quiet period only stops fresh announcements from flapping.
        pending = (not old.get("complete", True)) and str(old.get("key", "")).startswith("recovered")
        if not pending and (old.get("status") != "degraded" or delivered >= cap or age < quiet):
            st["health_notice"] = dict(notices["healthy"], status=status, affected=bad,
                                      checked_at=now.isoformat())
            return
        key, recovery = f"recovered:{delivered+1}", True
    notice = notices.setdefault(key, dict(complete=False))
    if notice.get("complete"):
        st["health_notice"] = dict(notice, status=status, affected=bad,
                                  checked_at=now.isoformat())
        return
    text = (f"M16 FEED {status.upper()}: {len(bad)} monitored symbols have feed/history/analysis gaps. "
            "Live alerts may be missed or delayed; stale entries will NOT be sent. "
            "Polling is not tick-time execution.") if bad else (
            "M16 feed monitoring healthy on latest poll; polling latency still applies.")
    if recovery:
        text = ("M16 RECOVERED: feed and sector inputs are current again; entry alerts are unblocked. "
                "Stale entries were not sent and polling latency still applies.")
    if partial_sectors and not sector_unknown:
        if not bad:
            text = "Healthy-sector prices are current; sector coverage remains degraded."
        text = "M16 incomplete sectors: "+", ".join(partial_sectors)+". Affected sectors blocked; other alerts require guaranteed top-three rank despite missing sectors. " + text
    if sector_unknown:
        # Name which of the two states it is, when the last good board existed
        # and why the refresh is failing: a 30-second gap and an all-morning
        # outage read identically in the old wording.
        detail = []
        boards = st.get("sector_snapshots") or []
        if boards:
            detail.append("last board " + model.stamp(boards[-1]["close"]).strftime("%H:%M"))
        refresh = st.get("sector_health") or {}
        if refresh.get("reason") and not refresh.get("ok", True):
            detail.append("refresh failing: " + str(refresh["reason"])[:120])
        state = sector_reason.split("board ")[-1] if sector_reason else "unknown"
        text = ("M16 sector board missing/stale: entry alerts blocked until current persistence "
                "data is available (state: " + state +
                ("; " + "; ".join(detail) if detail else "") + "). ") + text
    if st.get("monitoring_health", {}).get("provider_throttled"):
        text += " Provider rate limit (HTTP 429); M16 is pausing requests and resumes automatically."
    if bad:
        text += "\n" + "\n".join(
            f"{sym}: {', '.join(details[sym]['reasons'])[:180]}; latest={details[sym]['latest']}; cursor={details[sym]['cursor']}"
            for sym in bad[:3])
    # Preserve successful destinations when retrying a partial send.
    ledger = notice.setdefault("deliveries", old.get("deliveries", {})
                               if old.get("status") == status else {})
    complete = True if dry_run else deliver_targets(st, ledger,
        lambda token, chat: tg.send_message_to(token, chat, text))
    notice.update(status=status, at=now.isoformat(), affected=bad, key=key,
                  deliveries=ledger, complete=complete)
    if recovery and complete:
        st["health_recoveries"] = int(st.get("health_recoveries", 0)) + 1
    # An unconfirmed recovery keeps the degraded marker so the next cycle
    # re-enters this branch and retries only the destinations still missing.
    st["health_notice"] = dict(notice, status="degraded" if recovery and not complete else status)
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


def fetch_candidates(symbols, fast, active, requested, busy, lanes, basket=frozenset()):
    """Bound admission by actual executor lane; oldest equal-priority work first."""
    slots = {True: max(0, 8-sum(lanes.values())),
             False: max(0, 16-sum(not lane for lane in lanes.values()))}
    for sym in sorted(symbols, key=lambda s: (s not in fast, s not in active, s not in basket, requested.get(s, -1e10), s)):
        lane = sym in fast
        if sym not in busy and slots[lane]:
            slots[lane] -= 1
            yield sym


def needs_discovery_refresh(cached, target):
    """A pre-close snapshot is not final merely because the loop clock advanced."""
    closed = shared._closed_bars(cached[0], cached[1]) if cached else None
    return closed is None or closed.empty or model.stamp(closed.iloc[-1]["dt"]) < target


def needs_audit_refresh(cached, target, now):
    """Refresh post-cutoff audit symbols without spin-polling Yahoo's omitted 15:15/15:20 bars."""
    if not needs_discovery_refresh(cached, target):
        return False
    clock = model.stamp(now)
    day = clock.normalize()
    closing_gap_start = day + pd.Timedelta(hours=15, minutes=15)
    closing_final = day + pd.Timedelta(hours=15, minutes=25)
    if closing_gap_start <= model.stamp(target) < closing_final and cached:
        grace_done = clock.floor("5min") + pd.Timedelta(seconds=35)
        if model.stamp(cached[1]) >= grace_done:
            return False
    return True


def discovery_retries(symbols, latest, now, due=None, attempted=None):
    """Re-fetch gaps inside the freshness budget, not the whole universe.

    With a rotating slice, a symbol holding usable bars that are one rotation
    old is on schedule and must wait for its slice; retrying it every bucket
    would rebuild the full-universe wave the rotation removes. Three cases are
    still retried at once: symbols in the current slice, symbols that were
    attempted but never produced data, and any symbol with locally cached but
    not-yet-final bars. `due=None` keeps the historical whole-universe behaviour.
    """
    if model.before(now, "09:20:00") or not model.before(now, "11:00:00"):
        return set()
    target = (model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")-pd.Timedelta(minutes=5)
    attempted = attempted or set()
    due = set(symbols) if due is None else set(due)
    return {sym for sym in symbols
            if (sym in due or (sym not in latest and sym in attempted))
            and needs_discovery_refresh(latest.get(sym), target)}


def discovery_slice(symbols, bucket, slices=DEFAULT_DISCOVERY_SLICES):
    """Deterministic 1-in-N discovery rotation: full coverage every N buckets."""
    ordered = sorted(symbols)
    slices = max(1, int(slices))
    # Nothing to spread when the universe is no larger than the slice count.
    if slices == 1 or len(ordered) <= slices:
        return set(ordered)
    clock = model.stamp(bucket)
    step = (int(clock.hour) * 60 + int(clock.minute)) // 5
    return {sym for i, sym in enumerate(ordered) if i % slices == step % slices}


def discovery_hold(now, bucket_age, has_close_board, gate_required=True, refresh_pending=False):
    """Bound how long a fresh bucket's board may hold back the discovery wave.

    The board and the discovery wave share one provider budget and one 429
    cooldown. While the board's own refresh is still in flight the wave stays
    queued behind it; the wait is capped so a hung refresh cannot stall
    discovery. With the sector gate disabled there is nothing to protect.

    In advisory mode the board cannot block anything, so it only ever needs a
    short courtesy window (`M16_SECTOR_RUNWAY_ADVISORY_SECONDS`, default 8):
    every second it holds the wave is a second added to a real alert's latency.
    """
    if not gate_required or has_close_board or not refresh_pending:
        return False
    if not model.before(now, "12:35:00"):
        return False
    if model.advisory():
        runway = max(0., float(os.getenv("M16_SECTOR_RUNWAY_ADVISORY_SECONDS", "8")))
    else:
        runway = max(0., float(os.getenv("M16_SECTOR_RUNWAY_SECONDS", "25")))
    return bucket_age < runway


def running_snapshot_usable(bars, started, observed):
    receipt = model.stamp(bars.attrs.get("m16_feed", {}).get("received_at", started))
    return (0 <= (model.stamp(observed)-receipt).total_seconds() <= 5 and
            0 <= (receipt-model.stamp(started)).total_seconds() <= MAX_LAG)


def register_session_start(st, automatic=True):
    """Bound automatic chains without preventing an operator recovery start.

    Legacy total counts conservatively consume the automatic allowance. Never
    reset market state or delivery deduplication merely to restart monitoring.
    """
    limit = max(1, int(os.getenv("M16_MAX_SESSION_STARTS", "12")))
    count = st.get("automatic_session_starts", st.get("session_starts", 0))
    if automatic and count >= limit:
        raise SystemExit("M16 automatic restart cap reached; start a manual session to resume monitoring")
    st["automatic_session_starts"] = count + int(automatic)
    st["session_starts"] = st.get("session_starts", 0)+1
    st["last_session_start"] = dict(automatic=automatic, at=shared.now_ist().isoformat())
    print(f"M16 {'automatic' if automatic else 'manual recovery'} session start; "
          f"automatic allowance used {st['automatic_session_starts']}/{limit}; total starts {st['session_starts']}")


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
    open_wave_started = None
    outage_since = None
    # Provider-wide throttling (HTTP 429 / shared cooldown) is handled as one
    # global pause. Hammering the universe during a cooldown, escalating every
    # symbol's backoff and then requesting a restart is what cost 2026-11-26:
    # 14 automatic restarts, first usable discovery at 09:46 and no sector
    # board until 11:25.
    provider_pause_until, provider_pause_since, provider_pause_streak = 0.0, None, 0
    discovery_slices = max(1, int(os.getenv("M16_DISCOVERY_SLICES", str(DEFAULT_DISCOVERY_SLICES))))
    provider_pause_base = max(1., float(os.getenv("M16_PROVIDER_PAUSE_SECONDS", "10")))
    provider_pause_cap = max(provider_pause_base, float(os.getenv("M16_PROVIDER_MAX_PAUSE_SECONDS", "120")))
    provider_max_wait = max(60., float(os.getenv("M16_PROVIDER_MAX_WAIT_SECONDS", "1500")))
    (ROOT / ".m16-unhealthy").unlink(missing_ok=True) if not dry_run else None
    failures, retry_at = {}, {}
    fetch_lanes = {}
    dormant = inactive_symbols(cache) | set(st.get("inactive_symbols", []))
    universe_syms = [s for s in shared.L.SYMS if s not in dormant]
    basket_members = frozenset(s for g in shared.MEMBERS.values() for s in g) & frozenset(universe_syms)
    # Two workers: the one-shot warm-up (index history for 19 tickers, network)
    # must never sit in front of the 09:20 board refresh. On 2026-11-26 the
    # single worker was still warming when the first candle closed, so the board
    # only appeared at 11:25 and every morning alert expired waiting for it.
    sector_workers = max(2, int(os.getenv("M16_SECTOR_WORKERS", "2")))
    with ProcessPoolExecutor(max_workers=sector_workers, mp_context=multiprocessing.get_context("spawn")) as sector_pool, ThreadPoolExecutor(max_workers=16) as fetch_pool, ThreadPoolExecutor(max_workers=8) as watch_fetch, \
            ProcessPoolExecutor(max_workers=worker_count, mp_context=multiprocessing.get_context("spawn")) as discovery, ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as active_pool:
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
                for future in [f for f in list(engines) if f.done()]:
                    sym = engines.pop(future, None)
                    try:
                        _, rows = future.result()
                        apply(st, sym, rows, shared.now_ist())
                    except Exception as exc:
                        st["diagnostics"].append(dict(at=now.isoformat(), symbol=sym, reason=f"engine: {exc}"))
                audit_symbols = {s["symbol"] for s in st["setups"].values() if s["symbol"] not in dormant}
                final_bar = model.stamp(now).normalize() + pd.Timedelta(hours=15, minutes=25)
                missing_close_bars = {
                    sym for sym in audit_symbols
                    if not st["cursors"].get(sym) or model.stamp(st["cursors"][sym]) < final_bar
                }
                if dry_run or not model.before(now, "15:32:30") or (not missing_close_bars and not fetches and not engines):
                    for future, sym in list(engines.items()):
                        engines.pop(future, None)
                        try:
                            _, rows = future.result(timeout=5)
                            apply(st, sym, rows, shared.now_ist())
                        except Exception as exc:
                            st["diagnostics"].append(dict(at=now.isoformat(), symbol=sym, reason=f"engine: {exc}"))
                    expire_setups(st, now)
                    trim_diagnostics(st)
                    save(st, dry_run)
                    finish(st, dry_run)
                    break
            if model.before(now, "09:15:00"):
                time.sleep(.5)
                continue
            if open_wave_started is None and not model.before(now, "09:20:00"):
                open_wave_started = time.monotonic()
            if sector_future is not None and sector_future.done():
                try:
                    snapshot = sector_future.result()
                    boards = st.setdefault("sector_snapshots", [])
                    if boards and model.stamp(boards[-1]["close"]) == model.stamp(snapshot["close"]):
                        if len(snapshot.get("sectors", {})) >= len(boards[-1].get("sectors", {})):
                            boards[-1] = snapshot
                    else:
                        boards.append(snapshot)
                    st["sector_health"] = dict(
                        ok=True, active=True, complete=snapshot.get("complete", True),
                        missing_sectors=snapshot.get("missing_sectors", []),
                        dormant_members=snapshot.get("dormant_members", []),
                        at=now.isoformat(),
                    )
                except Exception as exc:
                    reason = str(exc)
                    if reason != st.get("sector_health", {}).get("reason"):
                        print("M16 sector refresh unavailable: "+reason)
                    # Keep a bounded trail of refresh failures so the EOD state
                    # and notices can say *why* the board was unavailable.
                    trail = st.setdefault("sector_refresh_failures", [])
                    last = trail[-1] if trail else {}
                    if (last.get("reason") != reason or not last.get("at") or
                            (model.stamp(now)-model.stamp(last["at"])).total_seconds() >= 60):
                        trail.append(dict(at=now.isoformat(), reason=str(reason)[:200]))
                        del trail[:-20]
                    st["sector_health"] = dict(ok=False, active=True, reason=reason, at=now.isoformat())
                sector_future = None
            first_close_ready = not model.before(now, "09:20:00")
            current_close = model.stamp(now).floor("5min")
            close_age = (model.stamp(now) - current_close).total_seconds()
            current_board = st.get("sector_snapshots", [])
            has_close_board = bool(current_board and model.stamp(current_board[-1]["close"]) == current_close)
            board_complete = bool(has_close_board and current_board[-1].get("complete", True))
            need_sector = (not has_close_board) or (not board_complete and close_age <= MAX_LAG)
            if (sector_future is None and first_close_ready and need_sector and close_age >= 1.0
                    and time.monotonic() >= sector_retry and time.monotonic() >= provider_pause_until
                    and model.before(now, "12:35:00")):
                target_bar = current_close - pd.Timedelta(minutes=5)
                sector_bars = {sym: item[0] for sym, item in latest.items()
                               if sym in basket_members and not needs_discovery_refresh(item, target_bar)}
                sector_future = (sector_pool.submit(m16_sector.refresh, now, sector_bars)
                                 if sector_bars else sector_pool.submit(m16_sector.refresh, now))
                sector_retry = time.monotonic() + (2.5 if has_close_board else 2.0)
            elif sector_future is None and not first_close_ready and st.get("sector_health", {}).get("phase") != "awaiting_first_close":
                st["sector_health"] = dict(ok=True, active=False, phase="awaiting_first_close", at=now.isoformat())
            elif sector_future is None and not model.before(now, "12:35:00") and st.get("sector_health", {}).get("phase") != "post_cutoff":
                st["sector_health"] = dict(
                    ok=True, active=False, phase="post_cutoff",
                    snapshots=len(current_board),
                    last_close=current_board[-1]["close"] if current_board else None,
                    at=now.isoformat(),
                )
            active = {s["symbol"] for s in st["setups"].values() if s["phase"] not in model.TERMINAL}
            fast = {s["symbol"] for s in st["setups"].values() if s["phase"] in {"armed", "intrabar"}}
            bucket = (model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")
            # Key discovery off the candle that has actually closed, not the
            # wall-clock bucket: otherwise 09:20:00 and 09:20:01 count as two
            # scans and the whole wave (or two rotation slices) fires twice at
            # the open, exactly when the sector board needs the budget.
            scan_key = (model.stamp(now)-pd.Timedelta(minutes=5)).floor("5min")
            scan = first_close_ready and last_discovery != scan_key and model.before(now, "11:00:00")
            if scan and discovery_hold(now, close_age, has_close_board,
                                       bool(st.get("sector_gate_required")),
                                       sector_future is not None and not sector_future.done()):
                scan = False
            slice_now = discovery_slice(universe_syms, scan_key, discovery_slices)
            symbols = set(slice_now) if scan else set()
            if scan:
                last_discovery = scan_key
            # Re-fetch missing/late discovery bars during the freshness budget.
            # A batch arriving with yesterday's/previous candle's data must not
            # silently lose a morning signal until the next full discovery.
            symbols.update(discovery_retries(universe_syms, latest, now, due=slice_now,
                                             attempted=set(st.get("feed_health", {}))))
            # Terminal/expired seeds stay under slower observation for the
            # full-day research audit, without creating new entries.
            audit_symbols = {s["symbol"] for s in st["setups"].values() if s["symbol"] not in dormant}
            day_last_bar = model.stamp(now).normalize() + pd.Timedelta(hours=15, minutes=25)
            target = min((model.stamp(now)-pd.Timedelta(seconds=1)).floor("5min")-pd.Timedelta(minutes=5), day_last_bar)
            # Rally validation and EOD collection consume completed candles;
            # only armed/running setups need best-effort 1s quote monitoring.
            symbols |= fast
            if first_close_ready:
                symbols |= {sym for sym in audit_symbols if needs_audit_refresh(latest.get(sym), target, now)}
                if model.before(now, "12:35:00"):
                    symbols |= {sym for sym in basket_members if needs_discovery_refresh(latest.get(sym), target)}
            # Recover missed morning discovery even after the seed cutoff.
            # apply/model still enforce original candle cutoffs and no backdating.
            for sym in universe_syms:
                cursor = st["cursors"].get(sym)
                if not model.before(now, "11:00:00") and (not cursor or model.stamp(cursor).strftime("%H:%M") < "10:50"):
                    symbols.add(sym)
            busy_fetch = set(fetches.values())
            # Admit only work that can run now. Do not hide an entire universe
            # in executor FIFO queues ahead of later urgent candle/quote work.
            clock = time.monotonic()
            provider_paused = clock < provider_pause_until
            eligible = {sym for sym in symbols if not provider_paused and clock >= retry_at.get(sym, 0) and
                        clock-requested.get(sym, -1e10) >= (poll if sym in fast else max(5., poll))}
            for sym in fetch_candidates(eligible, fast, active, requested, busy_fetch, fetch_lanes, basket_members):
                executor = watch_fetch if sym in fast else fetch_pool
                future = executor.submit(timed_fetch, sym)
                fetches[future] = sym
                fetch_lanes[future] = sym in fast
                requested[sym] = time.monotonic()
            for future in list(fetches):
                if not future.done():
                    continue
                sym = fetches.pop(future)
                fetch_lanes.pop(future, None)
                try:
                    raw, received = future.result()
                    bars = normalize(raw, st["date"])
                    if bars is not None and not bars.empty:
                        latest[sym] = (bars, received)
                        failures.pop(sym, None)
                        retry_at.pop(sym, None)
                        provider_pause_streak, provider_pause_since = 0, None
                        record_feed_success(st, sym, now, raw.attrs.get("m16_feed", {}))
                    else:
                        raise ValueError("current-session feed unavailable")
                except Exception as exc:
                    reason = str(exc)
                    record_feed_failure(st, sym, now, reason)
                    if provider_throttled(reason):
                        # Pause the whole request stream instead of replaying a
                        # 210-symbol wave into an active provider cooldown.
                        # Escalate once per pause window: every in-flight symbol
                        # of the same wave must not multiply the backoff.
                        mono = time.monotonic()
                        if mono >= provider_pause_until:
                            provider_pause_streak += 1
                            pause = min(provider_pause_cap,
                                        provider_pause_base * 2 ** min(provider_pause_streak - 1, 4))
                        else:
                            pause = provider_pause_until - mono
                        provider_pause_since = mono if provider_pause_since is None else provider_pause_since
                        provider_pause_until = max(provider_pause_until, mono + pause)
                        st["provider_throttle"] = dict(
                            at=now.isoformat(), streak=provider_pause_streak,
                            pause_seconds=round(pause, 3), reason=reason[:200],
                        )
                        retry_at[sym] = provider_pause_until
                        continue
                    failures[sym] = failures.get(sym, 0)+1
                    max_backoff = 5 if not model.before(now, "15:30:00") else 30
                    retry_at[sym] = time.monotonic()+min(max_backoff, 2**min(failures[sym], 5))
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
                worker_cache = dict(ok=True, day=st["date"]) if "eng_hist" in cache[sym] else cache[sym]
                engines[executor.submit(analyze, (sym, bars, worker_cache, cursor, received))] = sym
            flush(st, shared.now_ist(), dry_run)
            if time.monotonic() >= next_health:
                startup_grace = float(os.getenv("M16_STARTUP_HEALTH_GRACE_SECONDS", "75"))
                clock = time.monotonic()
                in_flight = bool(fetches or engines or sector_future is not None)
                wave_age = clock - (open_wave_started if open_wave_started is not None else started)
                if in_flight and (clock - started < startup_grace or (open_wave_started is not None and wave_age < startup_grace)):
                    next_health = clock + 2
                else:
                    due = slice_now if model.before(now, "11:00:00") else None
                    notify_health(st, now, dry_run, due=due)
                    monitored = st.get("monitoring_health", {})
                    watched = monitored.get("watched", [])
                    bad = monitored.get("affected", [])
                    widespread = len(watched) >= 3 and len(bad)/len(watched) >= .8
                    outage_since = (clock if outage_since is None else outage_since) if widespread and model.before(now, "15:20:00") else None
                    # A provider cooldown is not a stuck session. Restarting
                    # replays the same opening wave into the throttle and clears
                    # the in-process cooldown clock, which is exactly how
                    # 2026-11-26 burned 14 automatic starts before the first
                    # usable candle at 09:46. Wait it out in-process; only ask
                    # for a supervised restart if the throttle outlasts the
                    # provider budget.
                    provider_waiting = (provider_pause_since is not None and
                                        clock - provider_pause_since < provider_max_wait and
                                        (clock < provider_pause_until or provider_pause_streak > 0))
                    if outage_since is not None and provider_waiting:
                        outage_since = clock
                    if outage_since is not None and clock-outage_since >= float(os.getenv("M16_UNHEALTHY_EXIT_SECONDS", "300")):
                        save(st, dry_run)
                        if not dry_run:
                            (ROOT / ".m16-unhealthy").write_text(now.isoformat())
                        for pending in list(engines)+list(fetches):
                            pending.cancel()
                        raise SystemExit("M16 sustained widespread outage; requesting supervised restart")
                    next_health = clock + 30
            if time.monotonic() >= next_heartbeat:
                import m16_transport
                st["yahoo_budget"] = m16_transport.status()
                print("M16 progress " + json.dumps(dict(at=now.isoformat(),
                    processed_symbols=len(st["cursors"]), setups=len(st["setups"]),
                    events=len(st["events"]), pending_fetches=len(fetches), pending_engines=len(engines),
                    unhealthy=len(st.get("monitoring_health", {}).get("affected", [])),
                    yahoo_budget=st["yahoo_budget"],
                    provider_throttle=st.get("provider_throttle"),
                    sector_health=st.get("sector_health"),
                    cursor_times=pd.Series(list(st["cursors"].values()), dtype=str).str.slice(11,16).value_counts().to_dict())))
                next_heartbeat = time.monotonic()+60
            expire_setups(st, shared.now_ist())
            trim_diagnostics(st)
            if time.monotonic()-last_save >= 2:
                save(st, dry_run)
                last_save = time.monotonic()
            time.sleep(.1)
        end_now = shared.now_ist()
        if end_now.strftime("%Y-%m-%d") == st["date"] and not model.before(end_now, "15:31:00") and not st.get("eod_done"):
            for future, sym in list(engines.items()):
                engines.pop(future, None)
                try:
                    _, rows = future.result(timeout=5)
                    apply(st, sym, rows, shared.now_ist())
                except Exception as exc:
                    st["diagnostics"].append(dict(at=end_now.isoformat(), symbol=sym, reason=f"engine: {exc}"))
            expire_setups(st, end_now)
            trim_diagnostics(st)
            save(st, dry_run)
            finish(st, dry_run)
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


def sector_probe():
    """Read-only end-to-end board diagnostic; never authorizes live entries."""
    now = model.stamp(shared.now_ist())
    close = min(now.floor('5min'), now.normalize()+pd.Timedelta(hours=15, minutes=30))
    try:
        snapshot = m16_sector.refresh(close)
        report = dict(ok=True, diagnostic_only=True, checked_at=now.isoformat(),
                      source_close=snapshot['close'], sectors=len(snapshot['sectors']), meta=snapshot['meta'],
                      complete=snapshot.get('complete', True),
                      coverage_status='degraded' if snapshot.get('missing_sectors') else 'complete',
                      missing_sectors=snapshot.get('missing_sectors', []),
                      missing_members=snapshot.get('missing_members', []),
                      dormant_members=snapshot.get('dormant_members', []),
                      rank_policy=snapshot.get('rank_policy', 'complete board'))
    except Exception as exc:
        report = dict(ok=False, diagnostic_only=True, checked_at=now.isoformat(), reason=str(exc))
    print(json.dumps(report, indent=2))
    return report['ok']


def next_regular_session_day(now=None):
    """Resolve the active or next open NSE equity session date in IST."""
    clock = model.stamp(now or shared.now_ist())
    probe = clock.normalize() + pd.Timedelta(hours=9, minutes=30)
    if clock.weekday() < 5 and clock.strftime("%H:%M") < "15:40" and m16_calendar.session_status(probe)[0] != "closed":
        return probe.strftime("%Y-%m-%d")
    for offset in range(1, 15):
        cand = probe + pd.Timedelta(days=offset)
        if cand.weekday() < 5 and m16_calendar.session_status(cand)[0] != "closed":
            return cand.strftime("%Y-%m-%d")
    raise RuntimeError("unable to locate next open NSE trading day within 14 days")


def previous_regular_session_day(target_day):
    """Resolve the immediately preceding open NSE equity session date in IST."""
    anchor = pd.Timestamp(f"{target_day} 09:30:00", tz="Asia/Kolkata")
    for offset in range(1, 15):
        cand = anchor - pd.Timedelta(days=offset)
        if cand.weekday() < 5 and m16_calendar.session_status(cand)[0] != "closed":
            return cand.strftime("%Y-%m-%d")
    return None


def readiness_check(target_day=None):
    """Read-only pre-session readiness verification; never mutates state or calls Telegram."""
    import re
    import yaml

    checked_at = model.stamp(shared.now_ist())
    day = next_regular_session_day(checked_at) if not target_day or target_day == "auto" else str(target_day).strip()
    session_probe = pd.Timestamp(f"{day} 09:30:00", tz="Asia/Kolkata")
    cal_status, cal_reason = m16_calendar.session_status(session_probe)
    calendar_ok = session_probe.weekday() < 5 and cal_status != "closed"
    expected_prior_day = previous_regular_session_day(day)

    cache = {sym: shared.symbol_history(sym, day) for sym in shared.L.SYMS}
    extras = {sym for sym, _ in extra_universe()}
    dormant = sorted(inactive_symbols(cache))
    active_syms = [sym for sym in shared.L.SYMS if sym not in dormant]
    # Extra-universe names are scan-only additions: their history arrives with
    # the next bootstrap, and until then they simply produce no seeds. They must
    # never block readiness (or the session would never start after a universe
    # change), so they are reported separately.
    missing_history = [sym for sym in active_syms if not cache[sym].get("ok") and sym not in extras]
    # Computed from the file, not from active_syms: an extra without history is
    # dormant by design and must still be visible here.
    extra_missing_history = sorted(sym for sym in extras if not (cache.get(sym) or {}).get("ok"))
    stale_prior_day = [
        sym for sym in active_syms if sym not in extras
        and expected_prior_day and str(cache[sym].get("last_day") or "")[:10] < expected_prior_day
    ]
    last_days = sorted({str(cache[sym].get("last_day") or "")[:10] for sym in active_syms if cache[sym].get("last_day")})

    basket_active = sorted({sym for grp in shared.MEMBERS.values() for sym in grp if sym not in dormant})
    basket_missing = [sym for sym in basket_active if not cache[sym].get("ok")]
    empty_baskets = sorted(sec for sec, grp in shared.MEMBERS.items() if not (set(grp) - set(dormant)))

    existing_state_date = None
    existing_eod_done = None
    if STATE.exists():
        try:
            raw_st = json.loads(STATE.read_text(encoding="utf-8"))
            existing_state_date = raw_st.get("date")
            existing_eod_done = raw_st.get("eod_done")
        except Exception:
            existing_state_date = "corrupt"
    rolled = load(day)
    json.dumps(rolled, allow_nan=False)
    cap = max(1, int(os.getenv("M16_MAX_SESSION_STARTS", "12")))
    used_starts = int(rolled.get("automatic_session_starts", rolled.get("session_starts", 0)))
    state_ready = (
        existing_state_date != "corrupt"
        and rolled.get("date") == day
        and not rolled.get("eod_done")
        and used_starts < cap
    )

    wf_path = ROOT / ".github/workflows/16_live_m16.yml"
    workflow_issues = []
    if wf_path.exists():
        wf_text = wf_path.read_text(encoding="utf-8")
        wf = yaml.safe_load(wf_text)
        job = wf["jobs"]["continuation"]
        steps = job["steps"]
        checkout = next((s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")), {})
        if checkout.get("with", {}).get("ref") != "main":
            workflow_issues.append("checkout ref is not main")
        if wf.get("concurrency", {}).get("cancel-in-progress") is not False:
            workflow_issues.append("cancel-in-progress is not false")
        if "actions/upload-artifact@v7" not in wf_text:
            workflow_issues.append("upload-artifact@v7 missing")
        crons = [item.get("cron") for item in (wf.get("on") or wf.get(True)).get("schedule", [])]
        if "25 3 * * 1-5" not in crons:
            workflow_issues.append(f"08:55 IST session cron missing (found {crons})")
        gate = re.search(r'hhmm < "(\d{2}:\d{2})" or hhmm >= "(\d{2}:\d{2})"', wf_text)
        if not gate or gate.groups() != ("08:55", "15:40"):
            workflow_issues.append("market gate window is not 08:55-15:40 IST")
        match = re.search(r"--session\s+--max-runtime-min\s+(\d+)", wf_text)
        runtime = int(match.group(1)) if match else 0
        timeout = int(job.get("timeout-minutes", 0))
        if runtime < 215 or timeout - runtime < 10:
            workflow_issues.append(f"invalid shift budget runtime={runtime} timeout={timeout}")
        handoff = next((s for s in steps if "Hand off" in s.get("name", "")), {})
        if "eod_done" not in handoff.get("run", ""):
            workflow_issues.append("handoff missing eod_done guard")
    else:
        workflow_issues.append("workflow file missing")
    workflow_ok = not workflow_issues

    ready = bool(
        calendar_ok
        and active_syms
        and not missing_history
        and not stale_prior_day
        and not basket_missing
        and not empty_baskets
        and state_ready
        and workflow_ok
    )
    report = dict(
        ready=ready,
        diagnostic_only=True,
        checked_at=checked_at.isoformat(),
        target_session=day,
        expected_prior_session=expected_prior_day,
        calendar=dict(ok=calendar_ok, status=cal_status, reason=cal_reason, weekday=session_probe.day_name()),
        universe=dict(
            total_symbols=len(shared.L.SYMS),
            active_symbols=len(active_syms),
            dormant_excluded=[sym for sym in dormant if sym not in extras],
            history_ok=sum(1 for sym in active_syms if cache[sym].get("ok")),
            extra_symbols=len(extras),
            missing_history=missing_history,
            extra_missing_history=extra_missing_history,
            stale_prior_day=stale_prior_day,
            active_last_days=last_days,
        ),
        sector_board=dict(
            total_sectors=len(set(shared.FM.OFFICIAL_TICKERS) | set(shared.MEMBERS)),
            official_indices=len(shared.FM.OFFICIAL_TICKERS),
            constituent_sectors=len(shared.MEMBERS),
            active_basket_members=len(basket_active),
            basket_missing_history=basket_missing,
            empty_baskets=empty_baskets,
        ),
        state_rollover=dict(
            ok=state_ready,
            on_disk_date=existing_state_date,
            on_disk_eod_done=existing_eod_done,
            rolled_date=rolled.get("date"),
            rolled_eod_done=bool(rolled.get("eod_done")),
            automatic_starts_used=used_starts,
            automatic_starts_cap=cap,
        ),
        workflow=dict(ok=workflow_ok, issues=workflow_issues),
        environment=dict(
            primary_telegram_configured=bool(os.getenv("M16_BOT_TOKEN") and os.getenv("M16_CHAT_ID")),
            mirror_telegram_configured=bool(os.getenv("M16_BOT_TOKEN_2") and os.getenv("M16_CHAT_ID_2")),
        ),
    )
    print(json.dumps(report, indent=2))
    return ready


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--feed-probe", action="store_true")
    parser.add_argument("--sector-probe", action="store_true")
    parser.add_argument("--readiness-check", nargs="?", const="auto", default=None,
                        help="Read-only pre-session readiness verification (optional YYYY-MM-DD, default next open session)")
    parser.add_argument("--max-runtime-min", type=float, default=324)
    parser.add_argument("--replay-date", help="Offline YYYY-MM-DD disk replay; no bot or feed calls")
    parser.add_argument("--symbols", nargs="+", default=None, help="Optional replay symbol subset")
    args = parser.parse_args()
    import m16_transport
    m16_transport.install()
    install_extra_universe()
    if args.readiness_check is not None:
        if not readiness_check(args.readiness_check):
            raise SystemExit(1)
        return
    if args.replay_date:
        result = replay(args.replay_date, args.symbols or list(shared.L.SYMS))
        print(pformat(result, width=120))
        return
    if args.sector_probe:
        if not sector_probe():
            raise SystemExit(1)
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
    if now.weekday() >= 5 or now.strftime("%H:%M") < "08:55" or now.strftime("%H:%M") >= "15:40":
        print("M16 outside weekday 08:55–15:40 IST window; no replay alerts")
        return
    if not args.dry_run:
        credentials()
    st = load(now.strftime("%Y-%m-%d"))
    if st.get("eod_done"):
        return
    st["sector_gate_required"] = True
    cache = {sym: shared.symbol_history(sym, st["date"]) for sym in shared.L.SYMS}
    dormant = sorted(inactive_symbols(cache))
    st["inactive_symbols"] = dormant
    for sym in dormant:
        st.get("feed_health", {}).pop(sym, None)
    if dormant:
        extras = {sym for sym, _ in extra_universe()}
        waiting = [sym for sym in dormant if sym in extras]
        market = [sym for sym in dormant if sym not in extras]
        parts = []
        if market:
            parts.append(f"{len(market)} dormant: {', '.join(market)}")
        if waiting:
            parts.append(f"{len(waiting)} extra-universe name(s) waiting for their first bootstrap history")
        print(f"M16 symbols excluded from live polling ({len(dormant)}): " + "; ".join(parts))
    st["history_health"] = {sym: bool(rec.get("ok")) for sym, rec in cache.items() if sym not in dormant}
    missing_history = [sym for sym, rec in cache.items() if sym not in dormant and not rec.get("ok")]
    if missing_history:
        st["diagnostics"].append(dict(at=now.isoformat(), reason="history unavailable", symbols=missing_history))
        print(f"M16 history unavailable for {len(missing_history)} symbols; recorded in diagnostics")
    if args.session:
        register_session_start(st, automatic=os.getenv("M16_AUTOMATIC_SESSION", "1") != "0")
        save(st, args.dry_run)
        live_session(st, cache, args.max_runtime_min, args.dry_run)
    else:
        if model.before(now, "09:20:00"):
            st["sector_health"] = dict(ok=True, active=False, phase="awaiting_first_close", at=now.isoformat())
            save(st, args.dry_run)
            print("M16 awaiting first 09:20 IST 5m candle close; state saved without premature feed polling")
            return
        if model.before(now, "12:35:00"):
            try:
                st.setdefault("sector_snapshots", []).append(m16_sector.refresh(shared.now_ist()))
            except Exception as exc:
                st["sector_health"] = dict(ok=False, active=True, reason=str(exc))
        else:
            boards = st.get("sector_snapshots", [])
            st["sector_health"] = dict(
                ok=True, active=False, phase="post_cutoff",
                snapshots=len(boards),
                last_close=boards[-1]["close"] if boards else None,
                at=now.isoformat(),
            )
        bars_map = {}
        active_syms = [s for s in shared.L.SYMS if s not in dormant]
        chosen = active_syms[:max(1, int(os.getenv("M16_DRY_RUN_SYMBOLS", "3")))] if args.dry_run else active_syms
        with ThreadPoolExecutor(max_workers=8) as pool:
            pending = {pool.submit(timed_fetch, sym): sym for sym in chosen}
            for future in as_completed(pending):
                sym = pending[future]
                try:
                    raw, _ = future.result()
                    bars_map[sym] = raw
                    record_feed_success(st, sym, shared.now_ist(), raw.attrs.get("m16_feed", {}))
                except Exception as exc:
                    record_feed_failure(st, sym, shared.now_ist(), str(exc))
        cycle(st, now, bars_map, cache, dry_run=args.dry_run)
        if not model.before(shared.now_ist(), "15:31:00"):
            finish(st, args.dry_run)


if __name__ == "__main__":
    main()
