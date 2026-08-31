"""Standalone M14 Ultra High-Conviction A+ Paper Runner (Guaranteed 1-3 trades/day)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import costs
import fast_feed
import feeds
import learn_log
import live_runner as L
import m11_runner as V
import m14_alerts as Alerts
import m14_entry as E
import m14_trader as T
import report
import flow_map as FM

ROOT = L.ROOT
STATE = ROOT / "state14.json"
PREV_CACHE = ROOT / "data" / "m14_prev_context.json"
SECTOR_OF = dict(pd.read_csv(ROOT / "fno_sector_map.csv").values)


def save_state(st: dict) -> None:
    STATE.write_text(json.dumps(st, indent=1))


def load_state(today: str) -> dict:
    if STATE.exists():
        try:
            st = json.loads(STATE.read_text())
            if st.get("date") == today:
                return st
        except Exception:
            pass
    return {
        "date": today,
        "signals": {},
        "trades": {},
        "alerts": [],
        "decisions": [],
        "eod_done": False,
        "cycles": 0,
        "prev_meta": {},
        "vix": {},
    }


def parse_day(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(s))
    except Exception:
        return None


def expected_prev(td: dt.date | None) -> dt.date | None:
    if not td:
        return None
    q = td - dt.timedelta(days=1)
    while q.weekday() >= 5:
        q -= dt.timedelta(days=1)
    return q


def finite(v) -> bool:
    try:
        return bool(pd.notna(float(v)) and np.isfinite(float(v)))
    except Exception:
        return False


def load_prev(today: str) -> tuple[dict[str, float], dict[str, float], dict]:
    """Strict previous-close source. A stale history file or cache never silently passes."""
    td = parse_day(today)
    exp = expected_prev(td)

    if PREV_CACHE.exists():
        try:
            j = json.loads(PREV_CACHE.read_text())
            c_date = parse_day(j.get("date"))
            vals = {k: float(v) for k, v in (j.get("close") or {}).items() if finite(v)}
            pivs = {k: float(v) for k, v in (j.get("pivot") or {}).items() if finite(v)}
            if c_date == exp and len(vals) >= 180:
                return vals, pivs, {
                    "status": "OK",
                    "source": PREV_CACHE.name,
                    "date": str(exp),
                    "count": len(vals),
                }
        except Exception:
            pass

    # Bootstrap from history files
    vals = {}
    piv = {}
    for sym in L.SYMS:
        try:
            h = pd.read_csv(L.HIST / f"{sym}.csv", usecols=["dt", "high", "low", "close"])
            h["day"] = h["dt"].astype(str).str[:10]
            q = h[h["day"] == str(exp)]
            if len(q):
                cl = float(q["close"].iloc[-1])
                vals[sym] = cl
                piv[sym] = (float(q["high"].max()) + float(q["low"].min()) + cl) / 3.0
        except Exception:
            continue

    if len(vals) >= 180:
        return vals, piv, {
            "status": "OK",
            "source": "data/history bootstrap",
            "date": str(exp),
            "count": len(vals),
        }

    # Strict Fail Closed: If date != expected previous weekday, do NOT pass as OK
    return {}, {}, {
        "status": "STALE",
        "source": "m14 cache/history",
        "expected": str(exp),
        "count": len(vals),
        "policy": "no entry; seed at EOD",
    }


def seed_prev(today: str, bars_map: dict[str, pd.DataFrame]) -> dict:
    vals = {}
    piv = {}
    last = []
    for sym, b in bars_map.items():
        if b is None or b.empty:
            continue
        q = b.sort_values("dt")
        late = q[q["t"] >= "15:20"]
        if late.empty:
            late = q.tail(1)  # Fallback to last available bar of session
        last_min = str(late["t"].iloc[-1])
        if last_min < "15:20":
            continue  # 15:10/15:15 mark is not an official session close
        cl = float(late["close"].iloc[-1])
        vals[sym] = cl
        piv[sym] = (float(q["high"].max()) + float(q["low"].min()) + cl) / 3.0
        last.append(last_min)

    j = {
        "date": today,
        "count": len(vals),
        "last_bar_min": min(last) if last else None,
        "total_fed": len(bars_map),
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    if len(vals) < 180:
        # Never overwrite a good cache with a partial session.
        j["status"] = "INSUFFICIENT"
        j["policy"] = "cache not written; next session fails closed until complete EOD seed"
        print(f"M14 EOD seed skipped: {len(vals)}/<180 symbols "
              f"(last bar {j['last_bar_min']}) — cache left untouched")
        return j
    j.update(close=vals, pivot=piv, status="OK")
    PREV_CACHE.parent.mkdir(exist_ok=True)
    PREV_CACHE.write_text(json.dumps(j, indent=1))
    return j


def prior_vix_return(today: str, st: dict) -> float | None:
    v = st.setdefault("vix", {})
    if v.get("date") == today and finite(v.get("prior_return")):
        return float(v["prior_return"])
    d = FM.fetch_index_bars(FM.VIX_TICKER, "5d")
    if d is None or d.empty:
        return 0.0
    d = d.copy()
    d["day"] = d["dt"].dt.strftime("%Y-%m-%d")
    daily = d[d["day"] < today].groupby("day")["close"].last().sort_index()
    if len(daily) < 2:
        return 0.0
    r = (float(daily.iloc[-1]) / float(daily.iloc[-2]) - 1.0) * 100.0
    v.update(date=today, prior_return=round(r, 4), last_day=str(daily.index[-1]))
    save_state(st)
    return r


def reserve(st: dict, key: str) -> bool:
    return Alerts.reserve_once(st, key, save_state)


def trade_count(st: dict) -> int:
    return sum(1 for t in st["trades"].values() if isinstance(t, dict) and "symbol" in t)


def open_count(st: dict) -> int:
    return sum(1 for t in st["trades"].values() if isinstance(t, dict) and "symbol" in t and not t.get("closed"))


def taken_symbols(st: dict) -> set[str]:
    return {t.get("symbol") for t in st["trades"].values() if isinstance(t, dict) and t.get("symbol")}


def taken_sectors(st: dict) -> set[str]:
    return {t.get("m14_sector") for t in st["trades"].values() if isinstance(t, dict) and t.get("m14_sector")}


def side_count(st: dict, side: str) -> int:
    return sum(1 for t in st["trades"].values() if isinstance(t, dict) and t.get("side") == side)


def full_losses(st: dict) -> int:
    return sum(1 for t in st["trades"].values() if isinstance(t, dict) and t.get("closed") and float(t.get("r_total", 0)) <= -0.8)


def next_key(st: dict, sym: str) -> str:
    if sym not in st["trades"]:
        return sym
    n = 2
    while f"{sym}#{n}" in st["trades"]:
        n += 1
    return f"{sym}#{n}"


def add_decision(st: dict, c: dict, taken: bool, why: str, trade_key: str | None = None) -> None:
    q = dict(c)
    q.pop("ts", None)
    q.update(taken=int(taken), why=why)
    if trade_key:
        q["trade_key"] = trade_key
    st["decisions"].append(q)


def warmup(sym: str, today: str) -> pd.DataFrame | None:
    return L.trader.load_warmup(L.HIST / f"{sym}.csv", today)


def manage(st: dict, today: str, bars_map: dict[str, pd.DataFrame], prev: dict[str, float]) -> None:
    for tkey in list(st["trades"]):
        old = st["trades"][tkey]
        sym = old.get("symbol")
        b = bars_map.get(sym)
        if not sym or b is None or b.empty:
            continue
        try:
            new = T.evaluate(
                sym,
                old["side"],
                old["time"],
                float(old["entry"]),
                old["signal"],
                b,
                warmup=warmup(sym, today),
                features=old.get("m14_features"),
            )
            if "error" in new:
                print(f"M14 manage {tkey}: {new['error']}")
                continue
            for k in ("m14_score", "m14_features", "m14_sector", "m14_code", "m14_subtype", "decision_id"):
                new[k] = old.get(k)
            st["trades"][tkey] = new
            for ev in new.get("events", []):
                key = f"{tkey}:{ev['key']}"
                if ev["key"] != "ENTRY" and reserve(st, key):
                    Alerts.send_message(T.fmt_alert(new, ev["key"]))
        except Exception as exc:
            print(f"M14 manage {tkey}: {type(exc).__name__}: {exc}")


def collect(st: dict, today: str, now: dt.datetime, bars_map: dict[str, pd.DataFrame],
            prev: dict[str, float], piv: dict[str, float], vixret: float | None) -> list[dict]:
    fresh = []
    params = L.ms.Params(enable_buy_ex10=False, enable_buy_ex11=False)
    known_ids = {str(x.get("decision_id")) for x in st.get("decisions", [])}

    for sym, tbars in bars_map.items():
        n = len(tbars)
        cursor = st["signals"].get(sym, {})
        if "nbars" in cursor:
            known = int(cursor.get("nbars", 0))
        else:
            closed = [k for k, x in enumerate(tbars["dt"]) if pd.Timestamp(x) + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
            known = closed[-1] if closed else 0
        known = min(known, max(n - 1, 0))

        for j in range(known, n):
            tk = pd.Timestamp(tbars["dt"].iloc[j])
            tk = tk.tz_localize(now.tz) if tk.tzinfo is None else tk
            if tk + pd.Timedelta(minutes=5) > pd.Timestamp(now):
                break
            try:
                prefix = tbars.iloc[: j + 1]
                frame = L.engine_frame(L.HIST / f"{sym}.csv", prefix, today)
                er = L.ms.run_symbol(frame, params).iloc[-1]
                st["signals"].setdefault(sym, {})["nbars"] = j + 1
            except Exception as exc:
                print(f"M14 engine {sym}: {exc}")
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

            detected = pd.Timestamp(L.now_ist())
            age = max(0.0, (detected - (tk + pd.Timedelta(minutes=5))).total_seconds() / 60.0)
            sector = str(SECTOR_OF.get(sym, "UNMAPPED"))
            spurt_rank = float(er.get("spurt_rank", er.get("gate_rank", 999.0)))

            base = {
                "decision_id": did,
                "symbol": sym,
                "side": side,
                "signal": signal,
                "code": code,
                "time": etime,
                "entry": round(float(tbars["close"].iloc[j]), 4),
                "sector": sector,
                "bar_dt": tk.isoformat(),
                "signal_age_min": round(age, 3),
                "spurt_rank": spurt_rank,
            }

            try:
                ctx = E.causal_price_features(frame, prefix, side, prev.get(sym),
                                               sector_breadth_prev_dir=None,
                                               video_setups=V.video_setups(prefix, j, side, piv.get(sym)) if etime >= E.ENTRY_START else [])
                ob, ncov = E.opening_breadth(bars_map, prev, side)
                vix_val = vixret if vixret is not None else 0.0
                setups = V.video_setups(prefix, j, side, piv.get(sym)) if etime >= E.ENTRY_START else []
                stats_regime = ctx.get("day_regime", "MIXED")

                feats = E.causal_features(
                    frame, prefix, side, prev.get(sym), spurt_rank,
                    er.get("totalScore", 0.0), ob, ob, ctx.get("sector_breadth_prev_dir"),
                    vix_val, setups, day_regime=stats_regime
                )
                dec = E.decide(code, signal, side, etime, feats)
                accepted = dec.accepted
                reason = dec.reason

                if accepted and age > E.MAX_SIGNAL_AGE_MIN:
                    accepted = False
                    reason = f"stale signal {age:.2f}m > {E.MAX_SIGNAL_AGE_MIN:.1f}m"

                base.update(score=dec.score, accepted=accepted, model_reason=reason, subtype=dec.subtype, features=feats)
            except Exception as exc:
                base.update(score=0.0, accepted=False, model_reason=f"feature failure: {type(exc).__name__}: {exc}", subtype="UNKNOWN", features={})

            fresh.append(base)
            known_ids.add(did)
    return fresh


def dispatch(st: dict, today: str, candidates: list[dict], bars_map: dict[str, pd.DataFrame], prev: dict[str, float]) -> None:
    if not candidates:
        return
    f = pd.DataFrame(candidates)
    f["ts"] = pd.to_datetime(f["bar_dt"], utc=True)

    # First pass: dispatch strictly qualified candidates (A+ score >= 70) up to 3 max per day
    for _ts, g in f.sort_values(["ts", "score", "symbol"], ascending=[True, False, True]).groupby("ts", sort=True):
        entered_this_bar = False
        for c in g.sort_values(["score", "symbol"], ascending=[False, True]).to_dict("records"):
            if not c["accepted"]:
                add_decision(st, c, False, c["model_reason"])
                continue

            why = None
            if entered_this_bar:
                why = "only one new M14 entry per 5-minute bar"
            elif trade_count(st) >= E.MAX_TRADES_PER_DAY:
                why = f"hard {E.MAX_TRADES_PER_DAY}-trade daily cap"
            elif open_count(st) >= E.MAX_CONCURRENT:
                why = f"maximum {E.MAX_CONCURRENT} concurrent positions"
            elif full_losses(st) >= 2:
                why = "two full-risk daily losses reached"
            elif c["symbol"] in taken_symbols(st):
                why = "one trade per symbol/day"
            elif E.ONE_TRADE_PER_SECTOR_DAY and c["sector"] in taken_sectors(st):
                why = "one trade per sector/day"
            elif side_count(st, c["side"]) >= E.MAX_TRADES_PER_SIDE:
                why = "side cap reached"

            if why:
                add_decision(st, c, False, why)
                continue

            try:
                cbars = bars_map[c["symbol"]]
                tr = T.evaluate(
                    c["symbol"],
                    c["side"],
                    c["time"],
                    float(c["entry"]),
                    c["signal"],
                    cbars,
                    warmup=warmup(c["symbol"], today),
                    features=c["features"],
                )
                if "error" in tr:
                    add_decision(st, c, False, f"trader rejected: {tr['error']}")
                    continue

                tr.update(
                    m14_score=float(c["score"]),
                    m14_features=c["features"],
                    m14_sector=c["sector"],
                    m14_code=int(c["code"]),
                    m14_subtype=c["subtype"],
                    decision_id=c["decision_id"],
                )
                tkey = next_key(st, c["symbol"])
                st["trades"][tkey] = tr
                add_decision(st, c, True, "selected", tkey)

                if reserve(st, f"{tkey}:ENTRY"):
                    Alerts.send_message(T.fmt_alert(tr, "ENTRY"))
                entered_this_bar = True
                print(f">>> M14 ENTRY {tkey} {c['side']} {c['signal']} score={c['score']:.1f} {c['subtype']}")
            except Exception as exc:
                add_decision(st, c, False, f"entry failure: {type(exc).__name__}: {exc}")

    # Fallback Guarantee: If ZERO trades taken today, select the single #1 highest-scoring real candidate of the day
    if trade_count(st) == 0:
        real_cand = [c for c in candidates if c.get("code") not in E.PREVIEW_CODES]
        if real_cand:
            top_cand = sorted(real_cand, key=lambda x: (x.get("score", 0.0), -x.get("spurt_rank", 999.0)), reverse=True)[0]
            try:
                cbars = bars_map[top_cand["symbol"]]
                tr = T.evaluate(
                    top_cand["symbol"],
                    top_cand["side"],
                    top_cand["time"],
                    float(top_cand["entry"]),
                    top_cand["signal"],
                    cbars,
                    warmup=warmup(top_cand["symbol"], today),
                    features=top_cand["features"],
                )
                if "error" not in tr:
                    tr.update(
                        m14_score=float(top_cand.get("score", 50.0)),
                        m14_features=top_cand.get("features", {}),
                        m14_sector=top_cand.get("sector", "UNMAPPED"),
                        m14_code=int(top_cand.get("code", 101)),
                        m14_subtype=top_cand.get("subtype", "MOMENTUM-CONTROLLED"),
                        decision_id=top_cand.get("decision_id"),
                    )
                    tkey = next_key(st, top_cand["symbol"])
                    st["trades"][tkey] = tr
                    add_decision(st, top_cand, True, "guaranteed daily top-conviction entry", tkey)

                    if reserve(st, f"{tkey}:ENTRY"):
                        Alerts.send_message(T.fmt_alert(tr, "ENTRY"))
                    print(f">>> M14 DAILY GUARANTEE ENTRY {tkey} {top_cand['side']} {top_cand['signal']} score={top_cand.get('score', 0.0):.1f}")
            except Exception as exc:
                print(f"M14 fallback entry error: {exc}")

    save_state(st)


def write_lab(st: dict, today: str, bars_map: dict[str, pd.DataFrame], prev: dict[str, float]) -> Path:
    rows = []
    for d in st.get("decisions", []):
        r = dict(d)
        feat = r.pop("features", {}) if isinstance(r.get("features"), dict) else {}
        r.update({f"f_{k}": v for k, v in feat.items()})
        b = bars_map.get(d.get("symbol"))
        if b is not None and not b.empty:
            try:
                tr = T.evaluate(
                    d["symbol"],
                    d["side"],
                    d["time"],
                    float(d["entry"]),
                    d["signal"],
                    b,
                    warmup=warmup(d["symbol"], today),
                    features=feat,
                )
                if "error" not in tr:
                    fee = costs.trade_costs(tr)
                    r.update(
                        cf_net=fee["net"],
                        cf_gross=fee["gross"],
                        cf_drag=fee["drag"],
                        cf_r=tr["r_total"],
                        cf_exit=tr["exit_text"],
                        cf_win=int(fee["net"] > 0),
                    )
            except Exception as exc:
                r["label_error"] = str(exc)
        rows.append(r)
    out = ROOT / "learn" / f"m14_candidates_{today}.csv"
    out.parent.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False)
    return out


def eod_ready(st: dict, hhmm: str) -> bool:
    return hhmm >= "15:25"


def finish_eod(st: dict, today: str, now: dt.datetime, bars_map: dict[str, pd.DataFrame], prev: dict[str, float]) -> None:
    done = [t for t in st["trades"].values() if isinstance(t, dict) and "symbol" in t]
    sk = {}
    for d in st.get("decisions", []):
        if d.get("taken"):
            continue
        sk.setdefault(d.get("why", "not selected"), []).append([d.get("symbol"), d.get("side"), d.get("signal"), d.get("time"), d.get("entry")])

    meta = {
        "status": E.MODEL_NAME,
        "source": "Top-10 Spurt × Video Setup × Regime Alignment × Guaranteed Daily Entry",
        "daily_cap": E.MAX_TRADES_PER_DAY,
        "prev": st.get("prev_meta"),
        "vix": st.get("vix"),
    }
    lbl = now.strftime("%d-%b-%Y") + " (M14 Guaranteed Daily High-Conviction A+)"
    out = report.build(
        done,
        lbl,
        meta,
        str(ROOT / f"paper_test_M14_{today}.xlsx"),
        skipped=sk or None,
        rules_note="M14 Guaranteed Daily High-Conviction A+ · 1-3 trades/day · structure SL ∓0.02% · ₹50k notional cap · ₹900 risk cap · +1R trail arming · 15:20 sq-off · full equity costs/slippage included",
    )
    lab = write_lab(st, today, bars_map, prev)
    learn_log.harvest("M14", today, st, None, bars_map)
    cache = seed_prev(today, bars_map)
    st["eod_done"] = True
    st["eod_cache_count"] = cache["count"]
    keys = [f"M14:{today}:EOD_SUMMARY", f"M14:{today}:EOD_DOCUMENT"]
    owned = Alerts.reserve_batch(st, keys, save_state)
    save_state(st)

    if keys[0] in owned:
        Alerts.send_message(f"🅼14 EOD · {report.summary_text(done, lbl, meta)}\nCandidates {len(st.get('decisions', []))} · trades {len(done)}/3")
    if keys[1] in owned:
        Alerts.send_document(out, caption=f"🅼14 📄 equity momentum report {today}")
    print(f"M14 EOD report={out} lab={lab}")


def mode_live() -> bool:
    now = L.now_ist()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    st = load_state(today)

    if st.get("eod_done"):
        print("M14 EOD done — idle")
        return False
    if hhmm < "09:16":
        save_state(st)
        print("M14 pre-market — idle")
        return False

    prev, piv, meta = load_prev(today)
    st["prev_meta"] = meta
    vix = prior_vix_return(today, st)

    bars_map = {}
    feed_cycle = fast_feed.FastFeedCycle()
    for sym in L.SYMS:
        try:
            b, _ = feed_cycle.fetch(sym, L.SID[sym], now)
            if b is not None and not b.empty:
                b = b.sort_values("dt").drop_duplicates("dt").reset_index(drop=True)
                b["t"] = b["dt"].dt.strftime("%H:%M")
                bars_map[sym] = b
        except Exception as exc:
            print(f"M14 feed {sym}: {exc}")

    st["feed"] = {
        "dhan_calls": feed_cycle.dhan_calls,
        "yahoo_calls": feed_cycle.yahoo_calls,
        "fallback": feed_cycle.trip_reason,
        "fed": len(bars_map),
    }

    manage(st, today, bars_map, prev)

    if meta.get("status") == "OK" and vix is not None:
        dispatch(st, today, collect(st, today, now, bars_map, prev, piv, vix), bars_map, prev)
    else:
        print(f"M14 strict no-entry prev={meta} vix={vix}")

    if eod_ready(st, hhmm):
        finish_eod(st, today, now, bars_map, prev)

    st["cycles"] = int(st.get("cycles", 0)) + 1
    save_state(st)
    print(f"M14 cycle trades={trade_count(st)}/3 open={open_count(st)} candidates={len(st.get('decisions', []))} feeds={len(bars_map)}")
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--test-alert", action="store_true")
    a = ap.parse_args()

    if a.test_alert:
        Alerts.test_alert()
        raise SystemExit(0)

    for i in range(max(1, a.loop)):
        if not mode_live():
            break
        if i < a.loop - 1:
            time.sleep(240)
