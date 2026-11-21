"""M16 causal five-minute pullback/BOS state machine. No sector filters/orders."""
from __future__ import annotations
import pandas as pd

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
        setup["rally_trail"] = [{k: float(bar[k]) for k in ("close", "ema9", "ema10")}]
    return key


def emit(st, setup, kind, bar_time, price, now, live):
    # Several original labels may lead to the same continuation pattern.
    # Track every seed, but notify only once per symbol/side/BOS/pullback/bar.
    key = (f"{setup['symbol']}:{setup['side']}:{setup.get('pullback_bar')}:"
           f"{setup.get('bos')}:{kind}:{stamp(bar_time).isoformat()}")
    if any(e["id"] == key for e in st["events"]):
        return
    event = dict(id=key, symbol=setup["symbol"], side=setup["side"], kind=kind,
                 bar=stamp(bar_time).isoformat(), observed=stamp(now).isoformat(), price=float(price),
                 bos=setup.get("bos"), pullback=setup.get("pullback"), seed=setup["signal_bar"],
                 signal=setup["signal"], rally=setup.get("rally_evidence", {}), live=bool(live))
    st["events"].append(event)
    if live:
        st["outbox"][key] = dict(event=event, status="pending")


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
            s.update(phase="intrabar", trigger_bar=t.isoformat())
            emit(st, s, "RUNNING_BREAK", t, price, now, True)


def expire(st, now):
    if not before(now, "12:30:00"):
        for s in st["setups"].values():
            if s["phase"] not in TERMINAL:
                s.update(phase="expired", invalid_reason="confirmation cutoff reached (12:30 IST)")
