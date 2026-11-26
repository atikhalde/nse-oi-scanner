"""M16 persistence-only gate using M6's unmodified sector board engine."""
import math
from concurrent.futures import ThreadPoolExecutor
import pandas as pd


def stamp(t):
    t = pd.Timestamp(t)
    return t.tz_localize('Asia/Kolkata') if t.tzinfo is None else t.tz_convert('Asia/Kolkata')


def decision(st, symbol, side, now):
    if not st.get('sector_gate_required'):
        return dict(status='disabled')
    import m6_runner as shared
    now = stamp(now)
    eligible = [s for s in st.get('sector_snapshots', [])
                if stamp(s['observed']) <= now and stamp(s['close']) <= now]
    if not eligible:
        return dict(status='unknown', reason='sector board unavailable')
    snap = max(eligible, key=lambda s: stamp(s['close']))
    # Do not silently reuse an older candle once the next candle has closed.
    if stamp(snap['close']) != now.floor('5min') or stamp(snap['close']).date() != now.date():
        return dict(status='unknown', reason='sector board stale')
    sec = shared.SECTOR_OF.get(symbol)
    if symbol in snap.get('dormant_members', []):
        return dict(status='unknown', reason='symbol feed dormant', sector=sec)
    metrics = snap['sectors'].get(sec)
    if not metrics:
        return dict(status='unknown', reason='home sector unavailable', sector=sec)
    key = 'persist_bull' if side == 'BUY' else 'persist_bear'
    score = float(metrics[key])
    ordered = sorted(snap['sectors'], key=lambda s: -float(snap['sectors'][s][key]))
    leaders = ordered[:3]
    missing = snap.get('missing_sectors', [])
    rank = ordered.index(sec)+1
    worst_rank = rank+len(missing)
    evidence = dict(sector=sec, score=score, top3=leaders, known_rank=rank,
                    worst_rank=worst_rank, missing_sectors=missing,
                    close=snap['close'], observed=snap['observed'])
    if not math.isfinite(score) or score <= 70 or rank > 3:
        return dict(status='block', **evidence)
    if worst_rank > 3:
        return dict(status='unknown', reason='top-three rank uncertain with missing sector', **evidence)
    return dict(status='pass', **evidence)



def fetch_members(members, now):
    """Explicit Yahoo-only basket feed: never calls the shared Dhan fallback."""
    import m6_runner as S
    import m16_transport
    m16_transport.install()
    def one(sym):
        return sym, S._prep(S.feeds.fetch_bars_yahoo(sym, rng='1d', timeout=(2, 3)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, members))


def warm(day):
    """Prime the dedicated process before the first session candle closes."""
    import m6_runner as S
    import m16_transport
    m16_transport.install()
    members = sorted({s for group in S.MEMBERS.values() for s in group})
    indices = list(S.FM.OFFICIAL_TICKERS.values())+[S.FM.NIFTY_TICKER, S.FM.VIX_TICKER]
    return S.preload(day, members, indices, log=lambda _: None)


def dormant_members(history, stale_days=7):
    """Identify stale/delisted universe entries whose local history trails peers."""
    dates = [pd.Timestamp(rec['last_day']).normalize()
             for rec in history.values() if rec.get('ok') and rec.get('last_day') is not None]
    if not dates:
        return set()
    latest = max(dates)
    return {sym for sym, rec in history.items()
            if rec.get('ok') and rec.get('last_day') is not None
            and (latest - pd.Timestamp(rec['last_day']).normalize()).days > stale_days}


_DORMANT_BASELINE_READY: set[tuple[str, str, tuple[str, ...]]] = set()


def refresh(now, shared_bars=None):
    """Dedicated process: no M6 state, orders or Telegram functions called."""
    import m6_runner as S
    import m16_transport
    m16_transport.install()
    now = stamp(now)
    close = now.floor('5min')
    today = now.strftime('%Y-%m-%d')
    target = (close-pd.Timedelta(minutes=5)).strftime('%H:%M')
    members = sorted({s for group in S.MEMBERS.values() for s in group})
    indices = list(S.FM.OFFICIAL_TICKERS.values())+[S.FM.NIFTY_TICKER, S.FM.VIX_TICKER]
    # Validate global/official index publication BEFORE fetching 60+ basket
    # equities, so an early pre-publication check never wastes stock quota.
    with ThreadPoolExecutor(max_workers=2) as pool:
        idx_job = pool.submit(S.fetch_indices, indices)
        warm_job = pool.submit(S.preload, today, members, indices, log=lambda _: None)
        idx = idx_job.result()
        _, history_count = warm_job.result()
    if history_count != len(set(indices)):
        raise ValueError('sector index warm-up history unavailable')
    def is_current(frame):
        frame = S._today_only(S._prep(frame), today)
        return S._has_bar(S._closed_bars(frame, close), target)

    # Shared NIFTY/VIX and official-index inputs remain mandatory. Never
    # allow a missing global input to change all sector scores silently.
    bad_indices = [name for name in indices if not is_current(idx.get(name))]
    if bad_indices:
        raise ValueError(f'sector board global/index inputs missing for {target}: '+', '.join(bad_indices))
    history = {sym: dict(S.symbol_history(sym, today)) for sym in members}
    dormant = dormant_members(history)
    for sym in dormant:
        history[sym]['ok'] = False
    for sec, group in S.MEMBERS.items():
        dropped = tuple(sorted(set(group) & dormant))
        if dropped and (sec, today, dropped) not in _DORMANT_BASELINE_READY:
            S._SYNTH_Y_CACHE.pop((sec, today), None)
            _DORMANT_BASELINE_READY.add((sec, today, dropped))
    active_members = [sym for sym in members if sym not in dormant]
    reusable = {sym: shared_bars[sym] for sym in active_members
                if shared_bars and sym in shared_bars and is_current(shared_bars[sym])}
    needed = [sym for sym in active_members if sym not in reusable]
    fetched = fetch_members(needed, now) if needed else {}
    bars = {**fetched, **reusable}
    missing_members = [sym for sym in active_members if not is_current(bars.get(sym)) or not history[sym].get('ok')]
    missing_sectors = sorted(
        sec for sec, group in S.MEMBERS.items()
        if not (set(group) - dormant) or ((set(group) - dormant) & set(missing_members))
    )
    # Exclude the ENTIRE affected basket when an active member is missing.
    # decision() bounds that unknown rank via worst_rank = rank + len(missing).
    safe_bars = {sym: frame for sym, frame in bars.items()
                 if S.SECTOR_OF.get(sym) not in missing_sectors and sym not in dormant}
    try:
        board, meta = S.build_board(close, today, safe_bars, idx, history)
    finally:
        # _synth_input may populate a prior-day baseline before discovering
        # that all current members were excluded. Do not retain a baseline
        # built from incomplete history for the eventual recovery candle.
        for sec in missing_sectors:
            S._SYNTH_Y_CACHE.pop((sec, today), None)
    expected = set(S.FM.OFFICIAL_TICKERS) | set(S.MEMBERS)
    known = expected-set(missing_sectors)
    if board is None or set(board.sectors) != known:
        raise ValueError('sector board does not match validated current inputs')
    sectors = {sec: {key: float(metrics[key]) for key in ('persist_bull', 'persist_bear')}
               for sec, metrics in board.sectors.items()}
    if any(not math.isfinite(value) for metrics in sectors.values() for value in metrics.values()):
        raise ValueError('non-finite sector persistence')
    return dict(close=close.isoformat(), observed=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),
                sectors=sectors, meta=meta, complete=not missing_sectors,
                missing_sectors=missing_sectors, missing_members=missing_members,
                dormant_members=sorted(dormant),
                rank_policy='known_rank + missing_sector_count <= 3')
