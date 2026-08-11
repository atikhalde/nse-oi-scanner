#!/usr/bin/env python3
"""M12 Selective Reversion — new standalone <=5 trades/day paper model.

Existing M1-M11 files and states are untouched. M12 reuses only the verified scanner,
feed, cost, report, and trade-management plumbing. Entry decisions live in m12_entry.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

import live_runner as L
import costs
import feeds
import fast_feed
import learn_log
import m11_runner as V
import m12_entry as E
import report
import telegram_bot as tg
import trader

ROOT = L.ROOT
STATE = ROOT / "state12.json"
PREV_CACHE = ROOT / "data" / "m12_prev_close.json"
SECTOR_OF = dict(pd.read_csv(ROOT / "fno_sector_map.csv").values)
M12_INCLUDE_MAIN = True


def m12_targets() -> list[tuple[str, str]]:
    """Two optional extra targets, plus the main tg target handled by callers.

    Complete M12_* pairs are preferred. If a pair is absent, the matching M11_*
    pair is reused so the established three-bot setup works without duplicating
    secrets. Partial pairs never mix credentials.
    """
    out = []
    main_pair = (os.environ.get("TELEGRAM_BOT_TOKEN"),
                 os.environ.get("TELEGRAM_CHAT_ID"))
    for suffix in ("A", "B"):
        tok = os.environ.get(f"M12_BOT_TOKEN_{suffix}")
        chat = os.environ.get(f"M12_CHAT_ID_{suffix}")
        if not (tok and chat):
            tok = os.environ.get(f"M11_BOT_TOKEN_{suffix}")
            chat = os.environ.get(f"M11_CHAT_ID_{suffix}")
        if tok and chat and (tok, chat) != main_pair and (tok, chat) not in out:
            out.append((tok, chat))
    return out


def _send_m12(text: str, silent: bool = False) -> int:
    """Fan out one message to main + optional A/B bots; return target count."""
    extra = m12_targets()
    to_main = M12_INCLUDE_MAIN or not extra
    if to_main:
        tg.send_message(text, silent=silent)
    for tok, chat in extra:
        try:
            tg._post(f"{tg.API}/bot{tok}/sendMessage",
                     data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True,
                           "disable_notification": bool(silent)}, timeout=20)
        except Exception as exc:
            print(f"M12 extra target {chat}: message failed {type(exc).__name__}")
    return (1 if to_main else 0) + len(extra)


def _doc_m12(path: str, caption: str = "") -> int:
    """Fan out one report document to main + optional A/B bots."""
    extra = m12_targets()
    to_main = M12_INCLUDE_MAIN or not extra
    if to_main:
        tg.send_document(path, caption=caption)
    for tok, chat in extra:
        try:
            with open(path, "rb") as f:
                tg._post(f"{tg.API}/bot{tok}/sendDocument",
                         data={"chat_id": chat, "caption": caption,
                               "parse_mode": "HTML"},
                         files={"document": f}, timeout=60)
        except Exception as exc:
            print(f"M12 extra target {chat}: document failed {type(exc).__name__}")
    return (1 if to_main else 0) + len(extra)


def test_alert() -> int:
    n = _send_m12("🧪 🅼12 TEST — three-target fanout is connected. "
                  "This is an alert-path test only; no state or trade is created.")
    print(f"M12 test alert dispatched to {n} target(s): "
          f"main={'yes' if (M12_INCLUDE_MAIN or not m12_targets()) else 'no'} "
          f"extras={len(m12_targets())}")
    return n


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def reserve_alert_once(st: dict, key: str) -> bool:
    """Persist an at-most-once key before any network send.

    True means this caller owns the first/only send. False means a previous cycle
    already reserved it and absolutely no bot call may be attempted.
    """
    alerts = st.setdefault("alerts", [])
    if key in alerts:
        return False
    alerts.append(key)
    save_state(st)
    return True


def reserve_alert_batch(st: dict, keys: list[str]) -> set[str]:
    """Atomically reserve all new keys in one state write; return newly owned keys."""
    alerts = st.setdefault("alerts", [])
    new = {k for k in keys if k not in alerts}
    if new:
        alerts.extend(k for k in keys if k in new)
        save_state(st)
    return new


def load_state(today: str) -> dict:
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            if st.get("date") == today:
                return st
        except Exception:
            pass
    return {"date": today, "signals": {}, "trades": {}, "alerts": [],
            "decisions": [], "eod_done": False, "cycles": 0, "prev_meta": {}}


def _parse_day(s: str):
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        return None


def _expected_previous_weekday(td: dt.date | None) -> dt.date | None:
    if td is None:
        return None
    d = td - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def load_previous_closes(today: str) -> tuple[dict[str, float], dict]:
    """Strict previous-close source. A stale history file never silently passes."""
    td = _parse_day(today)
    expected = _expected_previous_weekday(td)
    if PREV_CACHE.exists():
        try:
            j = json.loads(PREV_CACHE.read_text())
            day = _parse_day(j.get("date"))
            vals = {k: float(v) for k, v in (j.get("close") or {}).items() if float(v) > 0}
            age = (td - day).days if td and day else 999
            if day == expected and len(vals) >= 180:
                return vals, {"status": "OK", "source": PREV_CACHE.name,
                              "date": str(day), "age_days": age, "count": len(vals)}
        except Exception:
            pass

    # Bootstrap fallback: use only a common, recent last history session.
    rows = {}
    last_days = []
    for sym in L.SYMS:
        fp = L.HIST / f"{sym}.csv"
        try:
            h = pd.read_csv(fp, usecols=["dt", "close"])
            h["day"] = h["dt"].astype(str).str[:10]
            h = h[h["day"] < today]
            if len(h):
                rows[sym] = (str(h["day"].iloc[-1]), float(h["close"].iloc[-1]))
                last_days.append(str(h["day"].iloc[-1]))
        except Exception:
            continue
    common = pd.Series(last_days).mode().iloc[0] if last_days else None
    day = _parse_day(common)
    age = (td - day).days if td and day else 999
    vals = {s: px for s, (d, px) in rows.items() if d == common and px > 0}
    if common and day == expected and len(vals) >= 180:
        return vals, {"status": "OK", "source": "data/history bootstrap",
                      "date": common, "age_days": age, "count": len(vals)}
    return {}, {"status": "STALE", "source": "previous-close cache/history",
                "date": common, "expected_previous_weekday": str(expected),
                "age_days": age, "count": len(vals),
                "policy": "strict no-entry; EOD will seed cache for next session"}


def load_previous_pivots(today: str) -> dict[str, float]:
    """Correct prior-session daily pivots for the S2 video detector."""
    td = _parse_day(today); expected = _expected_previous_weekday(td)
    if PREV_CACHE.exists():
        try:
            j = json.loads(PREV_CACHE.read_text())
            if _parse_day(j.get("date")) == expected:
                p = {k: float(v) for k, v in (j.get("pivot") or {}).items() if _finite_num(v)}
                if len(p) >= 180:
                    return p
        except Exception:
            pass
    out = {}
    for sym in L.SYMS:
        fp = L.HIST / f"{sym}.csv"
        try:
            h = pd.read_csv(fp, usecols=["dt", "high", "low", "close"])
            h["day"] = h["dt"].astype(str).str[:10]
            q = h[h["day"] == str(expected)]
            if len(q):
                out[sym] = (float(q["high"].max()) + float(q["low"].min())
                            + float(q["close"].iloc[-1])) / 3.0
        except Exception:
            continue
    return out


def _finite_num(v) -> bool:
    try:
        return bool(pd.notna(float(v)))
    except (TypeError, ValueError):
        return False


def seed_previous_close_cache(today: str, bars_map: dict[str, pd.DataFrame]) -> dict:
    vals, pivots = {}, {}
    last_times = []
    for sym, b in bars_map.items():
        if b is None or b.empty:
            continue
        allq = b.sort_values("dt")
        # A 15:10/15:15 mark is not an official previous-session close. Excluding
        # incomplete feeds makes tomorrow fail closed instead of using a stale proxy.
        late = allq[allq["t"] >= trader.SQOFF]
        if late.empty:
            continue
        close = float(late["close"].iloc[-1])
        vals[sym] = close
        pivots[sym] = (float(allq["high"].max()) + float(allq["low"].min()) + close) / 3.0
        last_times.append(str(late["t"].iloc[-1]))
    meta = {"date": today, "close": vals, "pivot": pivots, "count": len(vals),
            "last_bar_min": min(last_times) if last_times else None,
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
    PREV_CACHE.parent.mkdir(parents=True, exist_ok=True)
    PREV_CACHE.write_text(json.dumps(meta, indent=1))
    return meta


def trade_count(st: dict) -> int:
    return sum(1 for tr in st.get("trades", {}).values()
               if isinstance(tr, dict) and "symbol" in tr)


def taken_symbols(st: dict) -> set[str]:
    return {str(tr.get("symbol")) for tr in st.get("trades", {}).values()
            if isinstance(tr, dict) and tr.get("symbol")}


def taken_sectors(st: dict) -> set[str]:
    return {str(tr.get("m12_sector")) for tr in st.get("trades", {}).values()
            if isinstance(tr, dict) and tr.get("m12_sector")}


def side_counts(st: dict) -> dict[str, int]:
    out = {"BUY": 0, "SELL": 0}
    for tr in st.get("trades", {}).values():
        if isinstance(tr, dict) and tr.get("side") in out:
            out[tr["side"]] += 1
    return out


def next_trade_key(st: dict, sym: str) -> str:
    if sym not in st["trades"]:
        return sym
    i = 2
    while f"{sym}#{i}" in st["trades"]:
        i += 1
    return f"{sym}#{i}"


def fmt_alert(tr: dict, key: str) -> str:
    msg = "🅼12 · " + trader.fmt_alert(tr, key)
    if key == "ENTRY":
        f = tr.get("m12_features", {})
        msg += (f"\n🧠 M12 score {tr.get('m12_score', 0):.0f} · selective-reversion"
                f"\nanti-chase {float(f.get('dir_prev_pct', 0)):+.3f}% vs previous close"
                f" · sector {tr.get('m12_sector', '—')}"
                f"\nvideo: {f.get('video_setups') or '—'} · sector-direction breadth "
                f"{float(f.get('sector_breadth_prev_dir', 0)):.2f}"
                f"\nDaily cap: {E.MAX_TRADES_PER_DAY} (one symbol + one sector/day)")
    return msg


def add_skip(st: dict, c: dict, why: str) -> None:
    row = dict(c)
    row.pop("bar_dt_ts", None)
    row["taken"] = 0
    row["why"] = why
    st["decisions"].append(row)


def manage_trades(st: dict, today: str, bars_map: dict[str, pd.DataFrame]) -> None:
    for tkey in list(st.get("trades", {})):
        old = st["trades"][tkey]
        sym = old.get("symbol")
        b = bars_map.get(sym)
        if not sym or b is None:
            continue
        try:
            new = trader.evaluate(sym, old["side"], old["time"], float(old["entry"]),
                                  old["signal"], b,
                                  warmup=trader.load_warmup(L.HIST / f"{sym}.csv", today),
                                  sl_mode=old.get("sl_mode", "structure"))
            for k in ("m12_score", "m12_features", "m12_sector", "m12_code", "decision_id"):
                new[k] = old.get(k)
            st["trades"][tkey] = new
            for ev in new.get("events", []):
                akey = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and reserve_alert_once(st, akey):
                    # Key is durable before fanout: retries/restarts can never resend.
                    _send_m12(fmt_alert(new, ev["key"]))
        except Exception as exc:
            print(f"  M12 manage {tkey}: {type(exc).__name__}: {exc}")


def collect_candidates(st: dict, today: str, now, bars_map: dict[str, pd.DataFrame],
                       prev: dict[str, float], pivots: dict[str, float]) -> list[dict]:
    fresh = []
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    known_ids = {str(x.get("decision_id")) for x in st.get("decisions", [])}
    for sym, tbars in bars_map.items():
        n_today = len(tbars)
        cursor = st["signals"].get(sym, {})
        if "nbars" in cursor:
            known = int(cursor.get("nbars", 0))
        else:
            # First valid run may occur mid-session after a stale-context/bootstrap
            # recovery. Old bars are not executable and replaying every prefix is
            # extremely expensive. Start at only the newest closed bar.
            closed = [k for k, x in enumerate(tbars["dt"])
                      if pd.Timestamp(x) + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
            known = closed[-1] if closed else 0
        if known > n_today:
            known = 0
        for j in range(known, n_today):
            tk = pd.Timestamp(tbars["dt"].iloc[j])
            if tk.tzinfo is None:
                tk = tk.tz_localize(now.tz)
            if tk + pd.Timedelta(minutes=5) > pd.Timestamp(now):
                break
            try:
                prefix = tbars.iloc[:j + 1]
                frame = L.engine_frame(L.HIST / f"{sym}.csv", prefix, today)
                er = L.ms.run_symbol(frame, params).iloc[-1]
                st["signals"].setdefault(sym, {})["nbars"] = j + 1  # pointer first
            except Exception as exc:
                print(f"  M12 engine {sym}: {type(exc).__name__}: {exc}")
                break
            code = er.get("scan_code")
            if pd.isna(code) or int(code) not in L.MASTER_CODES:
                continue
            code = int(code)
            side = "BUY" if code < 200 else "SELL"
            etime = tk.strftime("%H:%M")
            signal = str(er.get("scan_name", code))
            did = f"{sym}|{side}|{etime}|{code}"
            if did in known_ids:
                continue
            detected_at = pd.Timestamp(L.now_ist())
            signal_age = max(0.0, (detected_at - (tk + pd.Timedelta(minutes=5))).total_seconds() / 60.0)
            base = {"decision_id": did, "symbol": sym, "side": side, "signal": signal,
                    "code": code, "time": etime, "entry": round(float(tbars["close"].iloc[j]), 4),
                    "sector": str(SECTOR_OF.get(sym, "UNMAPPED")), "bar_dt": tk.isoformat(),
                    "detected_at": detected_at.isoformat(), "signal_age_min": round(signal_age, 3)}
            try:
                ctx = E.market_sector_context_at(bars_map, prev, SECTOR_OF, sym,
                                                 base["sector"], side, tk)
                sb = ctx.get("sector_breadth_prev_dir")
                setups = V.video_setups(prefix, j, side, pivots.get(sym)) if etime >= E.ENTRY_START else []
                feat = E.causal_price_features(frame, prefix, side, prev.get(sym), sb,
                                               video_setups=setups)
                feat.update(ctx)  # shadow telemetry: market/sector breadth + lead/lag
                dec = E.decide(code, signal, side, etime, feat)
                accepted, reason = dec.accepted, dec.reason
                if accepted and signal_age > E.MAX_SIGNAL_AGE_MIN:
                    accepted = False
                    reason = (f"stale signal: detected {signal_age:.2f} minutes after bar close "
                              f"(max {E.MAX_SIGNAL_AGE_MIN:.1f}); no late/backfilled entry")
                base.update(score=dec.score, accepted=accepted, model_reason=reason,
                            features=feat)
            except Exception as exc:
                base.update(score=0.0, accepted=False,
                            model_reason=f"feature failure: {type(exc).__name__}: {exc}", features={})
            fresh.append(base)
            known_ids.add(did)
    return fresh


def dispatch_candidates(st: dict, today: str, candidates: list[dict],
                        bars_map: dict[str, pd.DataFrame]) -> None:
    if not candidates:
        return
    frame = pd.DataFrame(candidates)
    frame["bar_dt_ts"] = pd.to_datetime(frame["bar_dt"], utc=True)
    for _ts, group in frame.sort_values(["bar_dt_ts", "score", "symbol"],
                                        ascending=[True, False, True]).groupby("bar_dt_ts", sort=True):
        rows = group.sort_values(["score", "symbol"], ascending=[False, True]).to_dict("records")
        for c in rows:
            if not c["accepted"]:
                add_skip(st, c, c["model_reason"])
                continue
            count = trade_count(st)
            # Immediate A+ policy: no waiting, batching, or reserved time slots.
            # Five is only a hard safety ceiling, never a target to fill.
            if count >= E.MAX_TRADES_PER_DAY:
                add_skip(st, c, "hard daily safety cap reached")
                continue
            if c["symbol"] in taken_symbols(st):
                add_skip(st, c, "one trade per symbol per day")
                continue
            if E.ONE_TRADE_PER_SECTOR_DAY and c["sector"] in taken_sectors(st):
                add_skip(st, c, "one trade per sector per day")
                continue
            if side_counts(st).get(c["side"], 0) >= E.MAX_TRADES_PER_SIDE:
                add_skip(st, c, f"{c['side']} side cap reached")
                continue
            b = bars_map.get(c["symbol"])
            try:
                tr = trader.evaluate(c["symbol"], c["side"], c["time"], float(c["entry"]),
                                     c["signal"], b,
                                     warmup=trader.load_warmup(L.HIST / f"{c['symbol']}.csv", today))
                if "error" in tr:
                    add_skip(st, c, f"trader rejected: {tr['error']}")
                    continue
                tr.update(m12_score=float(c["score"]), m12_features=c["features"],
                          m12_sector=c["sector"], m12_code=int(c["code"]),
                          decision_id=c["decision_id"])
                tkey = next_trade_key(st, c["symbol"])
                st["trades"][tkey] = tr
                taken = dict(c); taken.pop("bar_dt_ts", None)
                taken.update(taken=1, why="selected", trade_key=tkey)
                st["decisions"].append(taken)
                entry_key = f"{tkey}:ENTRY"
                if reserve_alert_once(st, entry_key):
                    _send_m12(fmt_alert(tr, "ENTRY"))
                print(f"  >>> M12 ENTRY {tkey} {c['side']} {c['signal']} @ {c['entry']} "
                      f"score={c['score']:.0f} sector={c['sector']}")
            except Exception as exc:
                add_skip(st, c, f"entry failure: {type(exc).__name__}: {exc}")


def write_lab_labels(st: dict, today: str, bars_map: dict[str, pd.DataFrame]) -> Path:
    """Counterfactual EOD labels for audit only; never fed into today's decisions."""
    rows = []
    for d in st.get("decisions", []):
        row = dict(d)
        row.pop("bar_dt_ts", None)
        b = bars_map.get(d.get("symbol"))
        if b is not None:
            try:
                tr = trader.evaluate(d["symbol"], d["side"], d["time"], float(d["entry"]),
                                     d["signal"], b,
                                     warmup=trader.load_warmup(L.HIST / f"{d['symbol']}.csv", today))
                if "error" not in tr:
                    fee = costs.trade_costs(tr)
                    row.update(cf_net=fee["net"], cf_gross=fee["gross"], cf_drag=fee["drag"],
                               cf_r=tr["r_total"], cf_exit=tr["exit_text"],
                               cf_win=int(fee["net"] > 0))
            except Exception as exc:
                row["label_error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(row.get("features"), dict):
            feat = row.pop("features")
            row.update({f"f_{k}": v for k, v in feat.items()})
        rows.append(row)
    out = ROOT / "learn" / f"m12_candidates_{today}.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    return out


def eod_ready(st: dict, bars_map: dict[str, pd.DataFrame], hhmm: str) -> bool:
    if hhmm < "15:25":
        return False
    traded = taken_symbols(st)
    if not traded:
        return True
    complete = sum(1 for s in traded if s in bars_map and
                   (bars_map[s]["t"] >= trader.SQOFF).any())
    if complete == len(traded):
        return True
    # Last scheduled workflow may need to finalize, but the report flags incomplete rows.
    return hhmm >= "15:34"


def finish_eod(st: dict, today: str, now, bars_map: dict[str, pd.DataFrame]) -> None:
    done = [t for t in st["trades"].values() if isinstance(t, dict) and "symbol" in t]
    skipped = {}
    for d in st.get("decisions", []):
        if d.get("taken"):
            continue
        skipped.setdefault(d.get("why", d.get("model_reason", "not selected")), []).append(
            [d.get("symbol"), d.get("side"), d.get("signal"), d.get("time"), d.get("entry")])
    dlbl = now.strftime("%d-%b-%Y") + " (M12 Selective Reversion · max 5)"
    gate_meta = {"status": E.MODEL_NAME, "source": "causal anti-chase + EMA anti-extension",
                 "prev_close": st.get("prev_meta", {}), "daily_cap": E.MAX_TRADES_PER_DAY}
    out = report.build(done, dlbl, gate_meta, str(ROOT / f"paper_test_M12_{today}.xlsx"),
                       skipped=skipped or None,
                       rules_note=("M12 standalone · max 5/day · one symbol/sector/day · side cap 3 · "
                                   "frozen whitelist + previous-close anti-chase + EMA anti-extension · "
                                   "current trader.py exits/costs"))
    lab = write_lab_labels(st, today, bars_map)
    learn_log.harvest("M12", today, st, None, bars_map)
    cache = seed_previous_close_cache(today, bars_map)
    st["eod_done"] = True
    st["eod_cache_count"] = cache["count"]
    summary_key = f"M12:{today}:EOD_SUMMARY"
    document_key = f"M12:{today}:EOD_DOCUMENT"
    owned = reserve_alert_batch(st, [summary_key, document_key])
    save_state(st)  # eod_done is durable before either network operation
    if summary_key in owned:
        _send_m12("🅼12 EOD · " + report.summary_text(done, dlbl, gate_meta)
                  + f"\nCandidates audited: {len(st.get('decisions', []))} · next-day closes: {cache['count']}")
    if document_key in owned:
        _doc_m12(out, caption=f"🅼12 📄 M12 report {today}")
    print(f"M12 EOD: report={out} lab={lab} prev-cache={cache['count']}")


def mode_live() -> bool:
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    st = load_state(today)
    if st.get("eod_done"):
        print("M12: EOD done — idle")
        return False
    if hhmm < "09:16":
        print("M12: pre-market — idle")
        save_state(st)
        return False

    prev, prev_meta = load_previous_closes(today)
    st["prev_meta"] = prev_meta
    print(f"M12 previous-close source: {prev_meta}")

    bars_map = {}
    feed_cycle = fast_feed.FastFeedCycle()
    for sym in L.SYMS:
        try:
            b, _src = feed_cycle.fetch(sym, L.SID[sym], now)
            if b is not None and not b.empty:
                b = b.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
                b["t"] = b["dt"].dt.strftime("%H:%M")
                bars_map[sym] = b
        except Exception as exc:
            print(f"  M12 feed {sym}: {type(exc).__name__}: {exc}")
    st["feed"] = {"dhan_calls": feed_cycle.dhan_calls, "yahoo_calls": feed_cycle.yahoo_calls,
                  "fallback": feed_cycle.trip_reason, "fed": len(bars_map)}

    manage_trades(st, today, bars_map)
    if prev_meta.get("status") == "OK":
        pivots = load_previous_pivots(today)
        fresh = collect_candidates(st, today, now, bars_map, prev, pivots)
        dispatch_candidates(st, today, fresh, bars_map)
    else:
        print("M12 STRICT NO-ENTRY: previous closes are stale/unavailable")

    if eod_ready(st, bars_map, hhmm):
        finish_eod(st, today, now, bars_map)
    st["cycles"] = int(st.get("cycles", 0)) + 1
    save_state(st)
    print(f"M12 cycle done: trades={trade_count(st)}/{E.MAX_TRADES_PER_DAY} "
          f"decisions={len(st.get('decisions', []))} feeds={len(bars_map)}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--test-alert", action="store_true",
                    help="send one M12 fanout test without touching trading state")
    args = ap.parse_args()
    if args.test_alert:
        test_alert()
        raise SystemExit(0)
    for i in range(max(1, args.loop)):
        if not mode_live():
            break
        if i < args.loop - 1:
            time.sleep(240)
