"""M16 causal five-minute pullback/BOS state machine. Persistence-gated alerts; no orders."""
from __future__ import annotations
import pandas as pd
import m16_sector

IST = "Asia/Kolkata"
TERMINAL = {"confirmed", "failed", "invalid", "expired"}


def stamp(value):
    t = pd.Timestamp(value)
    return t.tz_localize(IST) if t.tzinfo is None else t.tz_convert(IST)


def before(t, cutoff):
    return stamp(t).strftime("%H:%M:%S") < cutoff


def fresh_state(day):
    return dict(date=day, cursors={}, setups={}, events=[], outbox={}, diagnostics=[], eod_done=False)


# Objective starting definition of a rally (mirrored for SELL).
RALLY_MIN_BARS = 3
RALLY_MIN_RETURN_PCT = 0.3
RALLY_MIN_EFFICIENCY = 0.65
RALLY_WINDOW = 6


def ema_holds(bar, sign):
    """Equality/touch is allowed; a close through EITHER EMA invalidates."""
    import math
    values = [bar.get(k) for k in ("close", "ema9", "ema10")]
    if any(v is None or not math.isfinite(float(v)) for v in values):
        return False
    c, e9, e10 = map(float, values)
    return sign*(c-e9) >= -1e-10 and sign*(c-e10) >= -1e-10


def rally_metrics(trail, sign):
    """Directional progress + efficiency + both EMA slopes, no future bars."""
    if len(trail) < RALLY_MIN_BARS:
        return dict(ready=False, reason="not enough rally candles")
    closes = [p["close"] for p in trail]
    moves = [b-a for a,b in zip(closes, closes[1:])]
    net = sign*(closes[-1]-closes[0])
    travel = sum(abs(m) for m in moves)
    progress = net / closes[0] * 100
    efficiency = net/travel if travel else 0.
    directional_steps = sum(sign*m > 0 for m in moves)
    slopes = all(sign*(trail[-1][f"ema{n}"]-trail[0][f"ema{n}"]) > 0 for n in (9,10))
    ready = (progress >= RALLY_MIN_RETURN_PCT and efficiency >= RALLY_MIN_EFFICIENCY
             and directional_steps >= 2 and slopes)
    return dict(ready=ready, bars=len(trail), progress_pct=progress,
                efficiency=efficiency, directional_steps=directional_steps, ema_slopes_ok=slopes)


def seed(st, symbol, code, name, bar, context):
    """All final chart codes, EX toggles enabled by runner. Five-bar causal anchor."""
    t = stamp(bar["dt"])
    if code in (90, 290) or not (code in {80, 280} or 101 <= code <= 112 or 201 <= code <= 220):
        return None
    if t.strftime("%Y-%m-%d") != st["date"] or not before(t + pd.Timedelta(minutes=5), "11:00:00"):
        return None
    key = f"{symbol}:{t.isoformat()}:{code}"
    if key in st["setups"]:
        return key
    buy = code < 200
    side = "BUY" if buy else "SELL"
    context = context.copy()
    if "dt" in context.columns:
        context["dt"] = context["dt"].map(stamp)
        context = context[(context["dt"] <= t) & (context["dt"].dt.date == t.date())]
        context = context.sort_values("dt").drop_duplicates("dt")
    if context.empty:
        context = pd.DataFrame([bar])
    tail = context.tail(5)
    anchor = float(tail["low"].min() if buy else tail["high"].max())
    peak = float(bar["high"] if buy else bar["low"])
    st["setups"][key] = dict(id=key, symbol=symbol, side=side, signal=name,
                              signal_bar=t.isoformat(), signal_price=float(bar["close"]),
                              anchor=anchor, peak=peak, peak_bar=t.isoformat(), phase="rally",
                              rally_evidence=dict(ready=False), rally_trail=[], last_checked_bar=t.isoformat())
    setup = st["setups"][key]
    if not ema_holds(bar, 1 if buy else -1):
        setup.update(phase="invalid", invalid_reason="original signal close violates EMA9/EMA10 or EMA data missing")
    else:
        # Historical context proves an existing rally; it never arms a
        # pre-signal pullback or bypasses EMA continuity after tracking begins.
        prior = context.copy()
        if {"dt", "close", "ema9", "ema10"}.issubset(prior.columns):
            prior["dt"] = prior["dt"].map(stamp)
            prior = prior[(prior["dt"] <= t) & (prior["dt"].dt.date == t.date())]
            prior = prior.sort_values("dt").drop_duplicates("dt").tail(RALLY_WINDOW)
            # Only a contiguous suffix ending at the signal can prove its rally.
            if len(prior):
                groups = prior["dt"].diff().ne(pd.Timedelta(minutes=5)).cumsum()
                prior = prior[groups == groups.iloc[-1]]
                if prior.iloc[-1]["dt"] != t:
                    prior = prior.iloc[:0]
            setup["rally_trail"] = [{k: float(row[k]) for k in ("close", "ema9", "ema10")}
                                    for _, row in prior.iterrows()]
        if not setup["rally_trail"]:
            setup["rally_trail"] = [{k: float(bar[k]) for k in ("close", "ema9", "ema10")}]
        setup["rally_evidence"] = rally_metrics(setup["rally_trail"], 1 if buy else -1)
        setup["rally_context_start"] = prior.iloc[0]["dt"].isoformat() if "prior" in locals() and len(prior) and "dt" in prior.columns else t.isoformat()
    return key


def emit(st, setup, kind, bar_time, price, now, live):
    # Several original labels may lead to the same continuation pattern.
    # Track every seed, but notify only once per symbol/side/BOS/kind/bar.
    bar_iso = stamp(bar_time).isoformat()
    same = next((e for e in st["events"]
                 if e["symbol"] == setup["symbol"] and e["side"] == setup["side"]
                 and e.get("bos") == setup.get("bos") and e["kind"] == kind
                 and e["bar"] == bar_iso), None)
    if same is not None:
        if setup["signal_bar"] != same.get("seed") or setup["signal"] != same.get("signal"):
            extra = dict(setup=setup["id"], signal=setup["signal"], seed=setup["signal_bar"])
            coincident = same.setdefault("coincident_original_signals", [])
            if extra not in coincident:
                coincident.append(extra)
                if same["id"] in st["outbox"]:
                    st["outbox"][same["id"]]["event"] = same
        return
    key = (f"{setup['symbol']}:{setup['side']}:{setup.get('pullback_bar')}:"
           f"{setup.get('bos')}:{kind}:{bar_iso}")
    timely = bool(live)
    gate = m16_sector.decision(st, setup['symbol'], setup['side'], now)
    # Failure notices are risk updates, not new entries.
    risk_update = kind in {'CLOSE_FAILED', 'CLOSE_UNAVAILABLE'}
    prior_eligible_running = any(e['symbol'] == setup['symbol'] and e['side'] == setup['side']
                                 and e['bar'] == stamp(bar_time).isoformat()
                                 and e['kind'] == 'RUNNING_BREAK' and e.get('live')
                                 for e in st['events'])
    if st.get('sector_gate_required') and risk_update:
        live = live and prior_eligible_running
    else:
        live = live and gate['status'] in {'pass', 'disabled'}
    event = dict(id=key, symbol=setup["symbol"], side=setup["side"], kind=kind,
                 bar=stamp(bar_time).isoformat(), observed=stamp(now).isoformat(), price=float(price),
                 bos=setup.get("bos"), pullback=setup.get("pullback"), seed=setup["signal_bar"],
                 signal=setup["signal"], rally=setup.get("rally_evidence", {}), live=bool(live), sector_gate=gate)
    st["events"].append(event)
    # A close can arrive before the sector worker publishes that same close.
    # Retain only timely close candidates for the existing 30s delivery budget;
    # never promote historical backfill or a blocked/running detection.
    wait_sector = (timely and gate['status'] == 'unknown' and
                   kind in {'CLOSE_ONLY', 'CLOSE_CONFIRMED', 'DIRECT_BOS_CONFIRMED'})
    if live or wait_sector:
        st["outbox"][key] = dict(event=event, status="pending", awaiting_sector=wait_sector)


def direct_original_bos(st, key, bar, context, now, max_lag=30):
    """Find a pre-existing rally/pullback at the original label's own BOS.

    Search at most 24 completed session candles using the SAME state machine.
    Synthetic pattern anchors are local only, never real indicator labels.
    Final-label direct alerts are close-confirmed, never retrospectively running.
    """
    original = st["setups"].get(key)
    required = {"dt", "open", "high", "low", "close", "ema9", "ema10"}
    if not original or original["phase"] != "rally" or not required.issubset(context.columns):
        return False
    t, now = stamp(bar["dt"]), stamp(now)
    end = t+pd.Timedelta(minutes=5)
    if end > now or not before(end, "11:00:00") or not ema_holds(bar, 1 if original["side"] == "BUY" else -1):
        return False
    ctx = context.copy()
    ctx["dt"] = ctx["dt"].map(stamp)
    ctx = ctx[(ctx["dt"] <= t) & (ctx["dt"].dt.strftime("%Y-%m-%d") == st["date"])].sort_values("dt").drop_duplicates("dt").tail(24).reset_index(drop=True)
    if len(ctx) < 5 or stamp(ctx.iloc[-1]["dt"]) != t:
        return False
    for start in range(len(ctx)-4):
        temp = fresh_state(st["date"])
        first = ctx.iloc[start].to_dict()
        code = 80 if original["side"] == "BUY" else 280
        candidate_key = seed(temp, original["symbol"], code, "PATTERN_ONLY", first, ctx.iloc[:start+1])
        candidate = temp["setups"].get(candidate_key)
        if not candidate or candidate["phase"] == "invalid":
            continue
        for i in range(start+1, len(ctx)-1):
            closed_bar(temp, original["symbol"], ctx.iloc[i].to_dict(), now, max_lag)
        if candidate["phase"] != "armed":
            continue
        # Validate the actual original-signal candle as the breakout, including
        # EMA continuity, pullback depth and missing-candle safeguards.
        closed_bar(temp, original["symbol"], bar, now, max_lag)
        if candidate["phase"] != "confirmed":
            continue
        original.update({field: candidate[field] for field in (
            "anchor", "peak", "peak_bar", "bos", "pullback_bar", "pullback", "pullback_low", "pullback_high", "rally_evidence")})
        original.update(phase="confirmed", direct_bos=True, pattern_start=stamp(first["dt"]).isoformat(),
                        last_checked_bar=t.isoformat())
        # An older indicator seed may already have alerted this same BOS close.
        same = next((e for e in st["events"] if e["symbol"] == original["symbol"] and
                     e["side"] == original["side"] and stamp(e["bar"]) == t and
                     e.get("bos") == original["bos"] and
                     e["kind"] in {"CLOSE_CONFIRMED", "CLOSE_ONLY", "DIRECT_BOS_CONFIRMED"}), None)
        if same:
            same.setdefault("coincident_original_signals", []).append(dict(setup=key, signal=original["signal"]))
            original["direct_bos_event"] = same["id"]
        else:
            fresh = 0 <= (now-end).total_seconds() <= max_lag
            emit(st, original, "DIRECT_BOS_CONFIRMED", t, float(bar["close"]), now, fresh)
        return True
    return False


def closed_bar(st, symbol, bar, now, max_lag=30):
    """Advance once on a completed candle. Replay records facts, never late alerts."""
    t, now = stamp(bar["dt"]), stamp(now)
    end = t + pd.Timedelta(minutes=5)
    if end > now or t.strftime("%Y-%m-%d") != st["date"]:
        return
    fresh = 0 <= (now-end).total_seconds() <= max_lag
    for s in st["setups"].values():
        if s["symbol"] != symbol or s["phase"] in TERMINAL or t <= stamp(s["signal_bar"]):
            continue
        if not before(end, "12:30:00"):
            s.update(phase="expired", invalid_reason="confirmation cutoff reached (12:30 IST)")
            continue
        previous = s.get("last_checked_bar", s["signal_bar"])
        if t <= stamp(previous):
            continue
        if s["phase"] != "intrabar" and t-stamp(previous) > pd.Timedelta(minutes=5):
            s.update(phase="invalid", invalid_reason="missing completed candle; cannot verify EMA continuity")
            continue
        s["last_checked_bar"] = t.isoformat()
        sign = 1 if s["side"] == "BUY" else -1
        c, hi, lo = (float(bar[k]) for k in ("close", "high", "low"))
        holds = ema_holds(bar, sign)
        if s["phase"] != "intrabar" and not holds:
            s.update(phase="invalid", invalid_reason=f"close through EMA9/EMA10 at {t.isoformat()} (or missing EMA data)")
            continue
        if s["phase"] == "intrabar":
            trigger = stamp(s["trigger_bar"])
            if t < trigger:
                continue
            if t == trigger:
                leg = sign * (s["bos"]-s["anchor"])
                depth = (s["bos"]-lo)/leg if sign == 1 else (hi-s["bos"])/leg
                intact = depth <= .5
                confirmed = sign * (c-s["bos"]) > 0 and intact and holds
                if not holds:
                    s["invalid_reason"] = "breakout candle close violates EMA9/EMA10"
                elif not intact:
                    s["invalid_reason"] = "breakout candle retraces more than 50% of rally"
                elif not confirmed:
                    s["invalid_reason"] = "breakout candle failed to close beyond BOS"
                s["phase"] = "confirmed" if confirmed else "failed"
                emit(st, s, "CLOSE_CONFIRMED" if confirmed else "CLOSE_FAILED", t, c, now, fresh)
                continue
            # Feed omitted the trigger candle. Never confirm from a different bar.
            s.update(phase="failed", invalid_reason="trigger candle unavailable; no false later-bar confirmation")
            emit(st, s, "CLOSE_UNAVAILABLE", trigger, c, now, False)
            continue
        if (lo <= s["anchor"] if sign == 1 else hi >= s["anchor"]):
            s.update(phase="invalid", invalid_reason="rally anchor broken")
            continue
        if s["phase"] == "armed":
            leg = sign * (s["bos"]-s["anchor"])
            depth = (s["bos"]-lo)/leg if sign == 1 else (hi-s["bos"])/leg
            if depth > .5:
                s.update(phase="invalid", invalid_reason="pullback retraced more than 50% of rally")
                continue
        if s["phase"] == "armed" and t > stamp(s["pullback_bar"]):
            if sign * (c-s["bos"]) > 0:
                s["phase"] = "confirmed"
                emit(st, s, "CLOSE_ONLY", t, c, now, fresh)
                continue
        if s["phase"] != "rally":
            continue
        trail = s.setdefault("rally_trail", [])
        trail.append({k: float(bar[k]) for k in ("close", "ema9", "ema10")})
        s["rally_trail"] = trail = trail[-RALLY_WINDOW:]
        extreme = hi if sign == 1 else lo
        if sign * (extreme-s["peak"]) > 0:
            # OHLC cannot establish intrabar order: don't arm on the same bar
            # that extends the extreme. Wait for a later, distinct pullback.
            s["peak"], s["peak_bar"] = extreme, t.isoformat()
            s["rally_evidence"] = rally_metrics(trail, sign)
            if s["rally_evidence"]["ready"]:
                s.pop("waiting_reason", None)
            continue
        if t <= stamp(s["peak_bar"]):
            continue
        if not s.get("rally_evidence", {}).get("ready", False):
            s["waiting_reason"] = "no directional rally before pullback (sideways/insufficient progress)"
            continue
        leg = sign * (s["peak"]-s["anchor"])
        if leg <= 0 or sign * (s["peak"]-s["signal_price"]) < .001 * s["signal_price"]:
            continue
        a, b = sorted((s["peak"]-sign*.236*leg, s["peak"]-sign*.382*leg))
        fib = lo <= b and hi >= a
        ema = [n for n in (9, 10) if pd.notna(bar.get(f"ema{n}")) and lo <= float(bar[f"ema{n}"]) <= hi]
        depth = (s["peak"]-lo) / leg if sign == 1 else (hi-s["peak"]) / leg
        if depth > .5:
            # The requested setup is a SMALL pullback, not a deep reversal.
            s.update(phase="invalid", invalid_reason="pullback retraced more than 50% of rally")
            continue
        if fib or ema:
            s.pop("waiting_reason", None)
            s.update(phase="armed", bos=s["peak"], pullback_bar=t.isoformat(),
                     pullback=" + ".join((["FIB_0.236_0.382"] if fib else []) + [f"EMA{n}" for n in ema]),
                     pullback_low=lo, pullback_high=hi)


def running_bar(st, symbol, bar, now):
    """Use the current observed close/last price, NOT the candle's historical wick."""
    t, now = stamp(bar["dt"]), stamp(now)
    end = t + pd.Timedelta(minutes=5)
    if t.strftime("%Y-%m-%d") != st["date"] or not (t <= now < end) or not before(end, "12:30:00"):
        return
    for s in st["setups"].values():
        if s["symbol"] != symbol or s["phase"] != "armed" or t <= stamp(s["pullback_bar"]):
            continue
        price = float(bar["close"])
        if (float(bar["low"]) <= s["anchor"] if s["side"] == "BUY" else float(bar["high"]) >= s["anchor"]):
            s.update(phase="invalid", invalid_reason="rally anchor broken during running candle")
            continue
        sign = 1 if s["side"] == "BUY" else -1
        leg = sign * (s["bos"]-s["anchor"])
        depth = (s["bos"]-float(bar["low"]))/leg if sign == 1 else (float(bar["high"])-s["bos"])/leg
        if depth > .5:
            s.update(phase="invalid", invalid_reason="pullback retraced more than 50% during running candle")
            continue
        if sign * (price-s["bos"]) > 0:
            if st.get("sector_gate_required") and m16_sector.decision(st, symbol, s["side"], now)["status"] == "unknown":
                s["awaiting_sector_running_at"] = now.isoformat()
                continue
            s.pop("awaiting_sector_running_at", None)
            s.update(phase="intrabar", trigger_bar=t.isoformat())
            emit(st, s, "RUNNING_BREAK", t, price, now, True)


def expire(st, now):
    if not before(now, "12:30:00"):
        for s in st["setups"].values():
            if s["phase"] not in TERMINAL:
                s.update(phase="expired", invalid_reason="confirmation cutoff reached (12:30 IST)")
