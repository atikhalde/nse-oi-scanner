#!/usr/bin/env python3
"""flow_map.py — Sector Flow Map Engine for MODEL 6.

EXACT mirror of the user's TradingView indicator
"NIFTY Institutional Core + Flow [v4.0 Compact Table]" (Strength.txt, Pine v6),
extended from 15 sectors to ALL sectors:
  * 17 official NSE sector indices (15 original + INFRA + CONSUMPTION) via Yahoo 5m
  * 13 synthetic equal-weight baskets covering every remaining F&O stock
    (fno_sector_map.csv is the single source of truth; every stock has 1 home)
Boards per bar: Bull/Bear flow scores (EMA-smoothed like the chart), Persistent +/-
top-3 leaders, qualification flags, net tilt. Gate helper entry_ok() implements the
user-approved rule set (24-Jul-2026: Q0=A Q1=A Q2=A Q3=B Q4=A):
  BUY  ⟸ sector side==BULL + qualBull + bull board top-2 + persist_bull top-3
  SELL ⟸ sector side==BEAR + qualBear + bear board top-2 + persist_bear top-3

Deliberate micro-deviations from Pine (disclosed):
  1. hold% windows use partial windows for the first ~40 min (Pine sma is na in
     warmup); identical after 09:55 IST.
  2. VIX fallback: if the VIX feed is missing, volMult = 1.0 (port behaviour);
     with VIX present it is clamp(live/14, 0.6, 2.5) exactly like the chart.
  3. Sector prev-close = previous session's last 5m close (≈ official close).
"""
import io
import json
import time

import numpy as np
import pandas as pd
import requests

# --------------------------------------------------------------- configuration
CFG = dict(
    sector_lookback=8,          # All-Day Hold Lookback (bars)
    sector_smooth_len=5,        # Flow Score EMA
    flow_min_score=55.0,        # RS Flow Min Score
    flow_swap_threshold=2.5,    # (board only; swap state not needed for gate)
    flow_swap_bars=2,
    bull_min_day_pct=0.20,
    bull_min_range_pos=60.0,
    bear_min_day_pct=0.20,
    bear_max_range_pos=40.0,
    flow_side_edge=4.0,
    recent_flow_bars=4,
    recent_hold_min=60.0,
    recent_move_min=0.07,       # 0.07 = 7 bps? NO — original Pine value 0.07 (7%)?? input 0.07 means 0.07%
    vol_scale_ref=14.0,
    early_rot_boost=8.0,
    early_rot_until_min=135,
    or_minutes=30,
    persist_day_ret_w=0.30,
    persist_rs_w=0.30,
    persist_pos_w=0.25,
    persist_base_w=0.15,
)

OFFICIAL_TICKERS = {
    "AUTO": "^CNXAUTO", "FIN": "^CNXFIN", "IT": "^CNXIT", "MET": "^CNXMETAL",
    "DEF": "NIFTY_IND_DEFENCE.NS", "ENE": "^CNXENERGY", "FMCG": "^CNXFMCG",
    "OIL": "NIFTY_OIL_AND_GAS.NS", "PHA": "^CNXPHARMA", "PSE": "^CNXPSE",
    "PSUBK": "^CNXPSUBANK", "BANK": "^NSEBANK", "REALTY": "^CNXREALTY",
    "CDUR": "NIFTY_CONSR_DURBL.NS", "MEDIA": "^CNXMEDIA",
    "INFRA": "^CNXINFRA", "CONSUMPTION": "^CNXCONSUM",
}
NIFTY_TICKER = "^NSEI"
VIX_TICKER = "^INDIAVIX"

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def fetch_index_bars(ticker, rng="5d", retries=2):
    """Yahoo 5m bars for an index ticker (same transport as feeds.fetch_bars_yahoo)."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=5m&range={rng}"
    for a in range(retries + 1):
        try:
            r = requests.get(url, headers=_UA, timeout=20)
            j = r.json()["chart"]["result"][0]
            ts = j.get("timestamp") or []
            q = j["indicators"]["quote"][0]
            df = pd.DataFrame({"dt": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata"),
                               "open": q["open"], "high": q["high"], "low": q["low"],
                               "close": q["close"], "volume": q.get("volume")})
            return df.dropna(subset=["close"]).reset_index(drop=True)
        except Exception as e:
            if a == retries:
                print(f"flow_map: yahoo index {ticker}: {type(e).__name__}: {e}")
                return None
            time.sleep(1.0)


# --------------------------------------------------------------- Pine helpers
def pos_in_range(v, hi, lo):
    return float(np.clip((v - lo) / (hi - lo) * 100.0, 0, 100)) if hi > lo else 50.0


def bull_map(v, lo, hi):
    return float(np.clip(100.0 * (v - lo) / (hi - lo), 0, 100)) if hi - lo > 1e-4 else 0.0


def bear_map(v, lo, hi):
    return float(np.clip(100.0 * (hi - v) / (hi - lo), 0, 100)) if hi - lo > 1e-4 else 0.0


def _ema(np_vals, span):
    return pd.Series(np_vals).ewm(span=span, adjust=False).mean().values


# --------------------------------------------------------------- stable-flow hold
# Port of Strength.txt stableFlow block (var stableFlowId / candidate / force release)
# plus a simpler timed variant. Default OFF — runners opt in via set_hold().
HOLD = dict(mode="off", bars=12)


def set_hold(mode="off", bars=12):
    """mode: 'off' | 'faithful' (exact stableFlow port) | 'timed' (hold N bars)."""
    HOLD["mode"] = mode
    HOLD["bars"] = int(bars)


def _stable_leader(pb, side, upto, cfg):
    """pb[name] -> dict(bull=, bear=, bq=, rq=) per-bar arrays.
    Mirrors the original: raw leader = top-ranked QUALIFIED sector;
    swap needs rival margin >= flow_swap_threshold (2.5) for flow_swap_bars (2)
    consecutive bars, or force-release when holder unqualified + rival margin.
    Returns (leader, leader_qualified_now)."""
    thr, need = cfg["flow_swap_threshold"], cfg["flow_swap_bars"]
    qkey = "bq" if side == "bull" else "rq"
    sid = None; squal = False; cand = None; cbars = 0; fbars = 0
    last_ql = {}
    for i in range(upto + 1):
        ranked = sorted(((n, float(v[side][i]), bool(v[qkey][i]))
                         for n, v in pb.items() if i < len(v[side])),
                        key=lambda x: -x[1])
        if not ranked:
            continue
        nxt = ((n, s, q) for n, s, q in ranked if q)
        raw_id, raw_score, raw_qual = next(nxt, (ranked[0][0], ranked[0][1], False))
        sc = {n: s for n, s, _ in ranked}
        last_ql = {n: q for n, _, q in ranked}
        if sid is None:
            sid, squal, cand, cbars, fbars = raw_id, raw_qual, raw_id, 0, 0
            continue
        stable_score = sc.get(sid, 0.0)
        stable_qual_now = last_ql.get(sid, False)
        qual_swap = (not stable_qual_now) and raw_qual
        score_swap = (raw_id != sid) and (raw_score >= stable_score + thr) and (raw_qual or not squal)
        force_cond = (raw_id != sid) and (not stable_qual_now) and (raw_score >= stable_score + thr)
        fbars = fbars + 1 if force_cond else 0
        if fbars >= need:
            sid, squal, cand, cbars, fbars = raw_id, raw_qual, raw_id, 0, 0
        elif score_swap or qual_swap:
            if cand == raw_id:
                cbars += 1
            else:
                cand, cbars = raw_id, 1
            if cbars >= need:
                sid, squal, cand, cbars = raw_id, raw_qual, raw_id, 0
        else:
            cand, cbars = sid, 0
            squal = stable_qual_now
    if sid is None:
        return None, False
    iu = min(upto, len(pb[sid][side]) - 1)
    return sid, bool(pb[sid][qkey][iu])


# --------------------------------------------------------------- core metric
def sector_metrics(o, h, l, c, e20, prev_close, day_open, or_high, or_low,
                   n_intra_series, vix, minutes_now, cfg=CFG):
    """Vectorized over today's bars. Returns per-bar dict of arrays.
    Mirrors sectorMetric() of Strength.txt incl. final EMA(5) score smoothing.
    e20: EMA(20) values aligned to today's bars (caller computes over FULL series
    including history so the warmup matches the continuous chart)."""
    v = max(0.6, min(2.5, vix / cfg["vol_scale_ref"]))
    n = len(c)
    day_ret = (c - prev_close) / prev_close * 100.0 if prev_close else np.zeros(n)
    intra = (c - day_open) / day_open * 100.0 if day_open else np.zeros(n)
    rs = intra - np.asarray(n_intra_series)
    rs_s = _ema(rs, cfg["sector_smooth_len"])
    pos = np.array([pos_in_range(c[i], np.max(h[: i + 1]), np.min(l[: i + 1])) for i in range(n)])
    or_ready = minutes_now >= cfg["or_minutes"] and or_high is not None and or_low is not None
    mins_today = np.array([i * 5 + 5 for i in range(n)])      # minutes since open per bar

    bull_raw = np.zeros(n); bear_raw = np.zeros(n)
    for i in range(n):
        lb = min(cfg["sector_lookback"], i + 1); rc = min(cfg["recent_flow_bars"], i + 1)
        hb_all = 100.0 * np.mean(c[i - lb + 1: i + 1] > e20[i - lb + 1: i + 1])
        hb_rec = 100.0 * np.mean(c[i - rc + 1: i + 1] > e20[i - rc + 1: i + 1])
        hr_all = 100.0 * np.mean(c[i - lb + 1: i + 1] < e20[i - lb + 1: i + 1])
        hr_rec = 100.0 * np.mean(c[i - rc + 1: i + 1] < e20[i - rc + 1: i + 1])
        rb = min(cfg["recent_flow_bars"], max(1, i))
        rret = (c[i] - c[i - rb]) / c[i - rb] * 100.0 if c[i - rb] else 0.0

        bs = bull_map(rs_s[i], -0.30 * v, 1.00 * v) * 0.20 + bull_map(day_ret[i], -0.20 * v, 1.50 * v) * 0.15 \
            + bull_map(rret, -0.25 * v, 0.90 * v) * 0.20 + pos[i] * 0.15 + hb_all * 0.10 + hb_rec * 0.20
        bs *= (0.70 if (rret < -cfg["recent_move_min"] and pos[i] < 65) else 1.0) \
            * (0.70 if c[i] < e20[i] else 1.0) * (0.80 if hb_rec < cfg["recent_hold_min"] else 1.0)

        br = bear_map(rs_s[i], -1.00 * v, 0.30 * v) * 0.20 + bear_map(day_ret[i], -1.50 * v, 0.20 * v) * 0.15 \
            + bear_map(rret, -0.90 * v, 0.25 * v) * 0.20 + (100.0 - pos[i]) * 0.15 + hr_all * 0.10 + hr_rec * 0.20
        br *= (0.70 if (rret > cfg["recent_move_min"] and pos[i] > 35) else 1.0) \
            * (0.70 if c[i] > e20[i] else 1.0) * (0.80 if hr_rec < cfg["recent_hold_min"] else 1.0)

        if or_ready and mins_today[i] <= cfg["early_rot_until_min"]:
            rs_acc = rs_s[i] - (rs_s[i - 1] if i >= 1 else rs_s[i])
            if c[i] > or_high and c[i] > e20[i] and rs_s[i] > 0 and rs_acc >= 0 and rret >= 0 and pos[i] >= 55:
                bs += cfg["early_rot_boost"]
            if c[i] < or_low and c[i] < e20[i] and rs_s[i] < 0 and rs_acc <= 0 and rret <= 0 and pos[i] <= 45:
                br += cfg["early_rot_boost"]
        bull_raw[i] = min(100.0, bs); bear_raw[i] = min(100.0, br)

    bull = _ema(bull_raw, cfg["sector_smooth_len"])
    bear = _ema(bear_raw, cfg["sector_smooth_len"])
    # persisted components exposed for the persistent leaderboard at the last bar
    return dict(bull=bull, bear=bear, day_ret=day_ret, rs_s=rs_s, pos=pos,
                e20=e20, vix_mult=v)


def compute_board(sectors_today, nifty_today, vix, minutes_now, cfg=CFG):
    """sectors_today entries: dict with keys
      o,h,l,c   — TODAY's bars sliced to the evaluation time (closed bars only)
      c_full    — close series including prior-day history (for EMA20 warmup)
      prev_close, day_open, or_high, or_low
    nifty_today: dict with key n_intra (NIFTY intraday-ret % per its today's bar).
    vix: latest value (fallback vol_ref -> mult 1.0). minutes_now: mins since 09:15.
    Returns board dict with per-sector last-bar metrics + leaderboards + net tilt."""
    n_intra = nifty_today["n_intra"]
    vix = vix if vix and vix > 0 else cfg["vol_scale_ref"]
    v = max(0.6, min(2.5, vix / cfg["vol_scale_ref"]))
    out = {}
    arrays = {}
    for name, s in sectors_today.items():
        k = min(len(s["c"]), len(n_intra))
        e20_full = _ema(np.asarray(s["c_full"]), 20)
        m = sector_metrics(s["o"][-k:], s["h"][-k:], s["l"][-k:], s["c"][-k:],
                           e20_full[-len(s["c"]):][-k:], s["prev_close"], s["day_open"],
                           s["or_high"], s["or_low"], n_intra[-k:], vix, minutes_now, cfg)
        i = k - 1
        if i < 0:
            continue
        arrays[name] = (m, np.asarray(s["c"][-k:]))
        bull, bear = float(m["bull"][i]), float(m["bear"][i])
        dayr, rsv, posv = float(m["day_ret"][i]), float(m["rs_s"][i]), float(m["pos"][i])
        c, e20 = s["c"][-k:], m["e20"]
        lb = min(cfg["sector_lookback"], i + 1); rc = min(cfg["recent_flow_bars"], i + 1)
        hb_rec = 100.0 * np.mean(c[i - rc + 1: i + 1] > e20[i - rc + 1: i + 1])
        hr_rec = 100.0 * np.mean(c[i - rc + 1: i + 1] < e20[i - rc + 1: i + 1])
        rb = min(cfg["recent_flow_bars"], max(1, i))
        rret = (c[i] - c[i - rb]) / c[i - rb] * 100.0 if c[i - rb] else 0.0
        side = 1 if bull >= bear else -1
        qual_bull = (dayr >= cfg["bull_min_day_pct"] and posv >= cfg["bull_min_range_pos"]
                     and rsv > 0 and rret >= cfg["recent_move_min"]
                     and bull >= bear + cfg["flow_side_edge"] and bull >= cfg["flow_min_score"])
        qual_bear = (dayr <= -cfg["bear_min_day_pct"] and posv <= cfg["bear_max_range_pos"]
                     and rsv < 0 and rret <= -cfg["recent_move_min"]
                     and bear >= bull + cfg["flow_side_edge"] and bear >= cfg["flow_min_score"])
        p_bull = bull_map(dayr, -0.20 * v, 1.50 * v) * cfg["persist_day_ret_w"] \
            + bull_map(rsv, -0.30 * v, 1.00 * v) * cfg["persist_rs_w"] \
            + posv * cfg["persist_pos_w"] + bull * cfg["persist_base_w"]
        p_bear = bear_map(dayr, -1.50 * v, 0.20 * v) * cfg["persist_day_ret_w"] \
            + bear_map(rsv, -1.00 * v, 0.30 * v) * cfg["persist_rs_w"] \
            + (100.0 - posv) * cfg["persist_pos_w"] + bear * cfg["persist_base_w"]
        out[name] = dict(bull=round(bull, 2), bear=round(bear, 2), side=side,
                         qual_bull=bool(qual_bull), qual_bear=bool(qual_bear),
                         persist_bull=round(p_bull, 2), persist_bear=round(p_bear, 2),
                         day_ret=round(dayr, 3), pos=round(posv, 1), rs=round(rsv, 3),
                         recent=round(rret, 3))

    bull_q = sorted(((n, d["bull"]) for n, d in out.items() if d["side"] == 1 and d["qual_bull"]),
                    key=lambda x: -x[1])
    bear_q = sorted(((n, d["bear"]) for n, d in out.items() if d["side"] == -1 and d["qual_bear"]),
                    key=lambda x: -x[1])
    pb = sorted(((n, d["persist_bull"]) for n, d in out.items()), key=lambda x: -x[1])[:3]
    pw = sorted(((n, d["persist_bear"]) for n, d in out.items()), key=lambda x: -x[1])[:3]
    tb = sum(d["persist_bull"] for d in out.values()); tw = sum(d["persist_bear"] for d in out.values())
    tilt = (tb - tw) / max(1, len(out))
    label = ("STRONG BULLISH" if tilt >= 8 else "BULLISH" if tilt >= 3 else
             "STRONG BEARISH" if tilt <= -8 else "BEARISH" if tilt <= -3 else "NEUTRAL")
    bull_top2 = {n for n, _ in bull_q[:2]}
    bear_top2 = {n for n, _ in bear_q[:2]}
    persist_bull_top3 = {n for n, _ in pb}
    persist_bear_top3 = {n for n, _ in pw}

    # ---------------- stable-flow hold computation (only when HOLD mode != off)
    hold_mode = HOLD["mode"]
    hold_bull = None; hold_bull_qual = False
    hold_bear = None; hold_bear_qual = False
    held_bull = set(); held_bear = set()
    if hold_mode != "off" and arrays:
        pbars = {}
        for n, (mm, carr) in arrays.items():
            ba, ra = mm["bull"], mm["bear"]
            dr, rss, pp = mm["day_ret"], mm["rs_s"], mm["pos"]
            nb = len(ba)
            rr = np.zeros(nb)
            for ii in range(nb):
                rbk = min(cfg["recent_flow_bars"], max(1, ii))
                rr[ii] = (carr[ii] - carr[ii - rbk]) / carr[ii - rbk] * 100.0 if carr[ii - rbk] else 0.0
            bq = ((dr >= cfg["bull_min_day_pct"]) & (pp >= cfg["bull_min_range_pos"]) & (rss > 0)
                  & (rr >= cfg["recent_move_min"]) & (ba >= ra + cfg["flow_side_edge"])
                  & (ba >= cfg["flow_min_score"]))
            rq = ((dr <= -cfg["bear_min_day_pct"]) & (pp <= cfg["bear_max_range_pos"]) & (rss < 0)
                  & (rr <= -cfg["recent_move_min"]) & (ra >= ba + cfg["flow_side_edge"])
                  & (ra >= cfg["flow_min_score"]))
            pbars[n] = dict(bull=ba, bear=ra, bq=bq, rq=rq)
        upto = max(len(vv["bull"]) for vv in pbars.values()) - 1
        if hold_mode == "faithful":
            hold_bull, hold_bull_qual = _stable_leader(pbars, "bull", upto, cfg)
            hold_bear, hold_bear_qual = _stable_leader(pbars, "bear", upto, cfg)
        else:  # timed: qualified on that side within last HOLD['bars'] bars, side unflipped
            for n, vv in pbars.items():
                iu = min(upto, len(vv["bull"]) - 1)
                qb = np.where(vv["bq"][: iu + 1])[0]
                if len(qb) and iu - qb[-1] <= HOLD["bars"] and vv["bull"][iu] >= vv["bear"][iu]:
                    held_bull.add(n)
                qr = np.where(vv["rq"][: iu + 1])[0]
                if len(qr) and iu - qr[-1] <= HOLD["bars"] and vv["bear"][iu] > vv["bull"][iu]:
                    held_bear.add(n)

    def _hold_ok(bside, sector, d):
        if hold_mode == "off":
            return False, ""
        if bside == "bull":
            if d["side"] != 1 or sector not in persist_bull_top3:
                return False, ""
            if hold_mode == "faithful":
                if sector == hold_bull:
                    tag = "STRONG" if hold_bull_qual else "REL"
                    return True, f"{sector} stableflow-HOLD-{tag} + Persist+ (score {d['bull']:.0f})"
            elif sector in held_bull:
                pool = sorted(((n, out[n]["bull"]) for n in ({n for n, _ in bull_q} | held_bull)),
                              key=lambda x: -x[1])
                if sector in {n for n, _ in pool[:2]}:
                    return True, f"{sector} timed-HOLD top2 + Persist+ (score {d['bull']:.0f})"
            return False, ""
        if d["side"] != -1 or sector not in persist_bear_top3:
            return False, ""
        if hold_mode == "faithful":
            if sector == hold_bear:
                tag = "STRONG" if hold_bear_qual else "REL"
                return True, f"{sector} stableflow-HOLD-{tag} + Persist- (score {d['bear']:.0f})"
        elif sector in held_bear:
            pool = sorted(((n, out[n]["bear"]) for n in ({n for n, _ in bear_q} | held_bear)),
                          key=lambda x: -x[1])
            if sector in {n for n, _ in pool[:2]}:
                return True, f"{sector} timed-HOLD top2 + Persist- (score {d['bear']:.0f})"
        return False, ""

    def entry_ok(side, sector):
        if sector is None or sector not in out:
            return False, "no sector home/board"
        d = out[sector]
        if side == "BUY":
            if d["side"] == 1 and d["qual_bull"] and sector in bull_top2 and sector in persist_bull_top3:
                return True, f"{sector} bull-top2 + Persist+ (score {d['bull']:.0f})"
            okh, whyh = _hold_ok("bull", sector, d)
            if okh:
                return True, whyh
            if d["side"] != 1 or not d["qual_bull"]:
                return False, f"sector {sector} not bull-qualified (bull {d['bull']:.0f}/bear {d['bear']:.0f})"
            if sector not in bull_top2:
                return False, f"sector {sector} bull-qualified but outside top-2 (board {', '.join(f'{n} {s:.0f}' for n, s in bull_q[:2]) or '—'})"
            return False, f"sector {sector} not on Persistent+ board"
        else:
            if d["side"] == -1 and d["qual_bear"] and sector in bear_top2 and sector in persist_bear_top3:
                return True, f"{sector} bear-top2 + Persist- (score {d['bear']:.0f})"
            okh, whyh = _hold_ok("bear", sector, d)
            if okh:
                return True, whyh
            if d["side"] != -1 or not d["qual_bear"]:
                return False, f"sector {sector} not bear-qualified (bull {d['bull']:.0f}/bear {d['bear']:.0f})"
            if sector not in bear_top2:
                return False, f"sector {sector} bear-qualified but outside top-2 (board {', '.join(f'{n} {s:.0f}' for n, s in bear_q[:2]) or '—'})"
            return False, f"sector {sector} not on Persistent- board"

    return dict(sectors=out, bull_q=bull_q, bear_q=bear_q,
                persist_bull=pb, persist_bear=pw, net_tilt=round(tilt, 1),
                tilt_label=label, vix_mult=round(v, 2),
                bull_top2=sorted(bull_top2), bear_top2=sorted(bear_top2),
                hold_mode=hold_mode, hold_bull=hold_bull, hold_bull_qual=hold_bull_qual,
                hold_bear=hold_bear, hold_bear_qual=hold_bear_qual,
                held_bull=sorted(held_bull), held_bear=sorted(held_bear),
                entry_ok=entry_ok)


def board_text(board):
    """Human/Telegram one-screen view of the current board."""
    b = board
    lines = [
        f"🧭 <b>SECTOR FLOW</b> · net tilt {b['net_tilt']:+} ({b['tilt_label']}) · VIX×{b['vix_mult']}",
        "🐂 qualified BULL: " + (" • ".join(f"{n} {s:.0f}" for n, s in b["bull_q"][:5]) or "—"),
        "🐻 qualified BEAR: " + (" • ".join(f"{n} {s:.0f}" for n, s in b["bear_q"][:5]) or "—"),
        "💪 Persistent+: " + (" • ".join(f"{n} {s:.0f}" for n, s in b["persist_bull"]) or "—"),
        "🩸 Persistent−: " + (" • ".join(f"{n} {s:.0f}" for n, s in b["persist_bear"]) or "—"),
    ]
    if b.get("hold_mode") not in (None, "off"):
        if b["hold_mode"] == "faithful":
            hb = f"{b['hold_bull']} ({'STRONG' if b['hold_bull_qual'] else 'REL'})" if b["hold_bull"] else "—"
            hr = f"{b['hold_bear']} ({'STRONG' if b['hold_bear_qual'] else 'REL'})" if b["hold_bear"] else "—"
        else:
            hb = "•".join(b["held_bull"]) or "—"
            hr = "•".join(b["held_bear"]) or "—"
        lines.append(f"🔒 HOLD[{b['hold_mode']}] bull: {hb} · bear: {hr}")
    return "\n".join(lines)
