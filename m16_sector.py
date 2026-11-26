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
    metrics = snap['sectors'].get(sec)
    if not metrics:
        return dict(status='unknown', reason='home sector unavailable', sector=sec)
    key = 'persist_bull' if side == 'BUY' else 'persist_bear'
    score = float(metrics[key])
    leaders = sorted(snap['sectors'], key=lambda s: -float(snap['sectors'][s][key]))[:3]
    return dict(status='pass' if math.isfinite(score) and score > 70 and sec in leaders else 'block',
                sector=sec, score=score, top3=leaders, close=snap['close'], observed=snap['observed'])


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


def refresh(now):
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
    # Warm index histories concurrently instead of serial network requests
    # inside build_board; overlap all history work with current-price fetching.
    with ThreadPoolExecutor(max_workers=4) as pool:
        idx_job = pool.submit(S.fetch_indices, indices)
        bars_job = pool.submit(fetch_members, members, now)
        warm_job = pool.submit(S.preload, today, members, indices, log=lambda _: None)
        idx, bars = idx_job.result(), bars_job.result()
        _, history_count = warm_job.result()
    if history_count != len(set(indices)):
        raise ValueError('sector index warm-up history unavailable')
    # Fail closed on incomplete inputs; M6's normal partial-board tolerance
    # must not silently change the persistence ranks used by this strategy.
    missing = []
    for name, frame in list(idx.items())+list(bars.items()):
        frame = S._today_only(S._prep(frame), today)
        if not S._has_bar(S._closed_bars(frame, close), target):
            latest = str(frame.iloc[-1]['dt']) if frame is not None and len(frame) else 'none'
            missing.append(f'{name}:latest={latest}')
    if missing:
        raise ValueError(f'sector board incomplete for {target}: '+ '; '.join(missing))
    if set(indices)-set(idx) or set(members)-set(bars):
        raise ValueError('sector board inputs missing')
    history = {sym: S.symbol_history(sym, today) for sym in members}
    if any(not rec.get('ok') for rec in history.values()):
        raise ValueError('sector member indicator history unavailable')
    board, meta = S.build_board(close, today, bars, idx, history)
    if board is None or len(board.sectors) != 30:
        raise ValueError('complete 30-sector board unavailable')
    sectors = {sec: {key: float(metrics[key]) for key in ('persist_bull', 'persist_bear')}
               for sec, metrics in board.sectors.items()}
    if any(not math.isfinite(value) for metrics in sectors.values() for value in metrics.values()):
        raise ValueError('non-finite sector persistence')
    return dict(close=close.isoformat(), observed=pd.Timestamp.now(tz='Asia/Kolkata').isoformat(),
                sectors=sectors, meta=meta)
