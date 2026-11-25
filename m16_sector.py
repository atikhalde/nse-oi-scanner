"""M16 persistence-only gate using M6's unmodified sector board engine."""
import math
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


def refresh(now):
    """Dedicated process: no M6 state, orders or Telegram functions called."""
    import m6_runner as S
    now = stamp(now)
    close = now.floor('5min')
    today = now.strftime('%Y-%m-%d')
    target = (close-pd.Timedelta(minutes=5)).strftime('%H:%M')
    members = sorted({s for group in S.MEMBERS.values() for s in group})
    indices = list(S.FM.OFFICIAL_TICKERS.values())+[S.FM.NIFTY_TICKER, S.FM.VIX_TICKER]
    idx = S.fetch_indices(indices)
    bars = S.fetch_symbols(members, now)
    # Fail closed on incomplete inputs; M6's normal partial-board tolerance
    # must not silently change the persistence ranks used by this strategy.
    for frame in list(idx.values())+list(bars.values()):
        frame = S._today_only(S._prep(frame), today)
        if not S._has_bar(S._closed_bars(frame, close), target):
            raise ValueError('sector board inputs stale/incomplete')
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
