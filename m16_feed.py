"""M16 Yahoo-only feed, using M6's Yahoo fetcher with freshness checks."""
import pandas as pd
import feeds


def current_bars(raw, now):
    if raw is None or raw.empty:
        raise ValueError('empty response')
    bars = raw.copy()
    bars['dt'] = pd.to_datetime(bars['dt'], utc=True).dt.tz_convert('Asia/Kolkata')
    clock = pd.Timestamp(now)
    bars = bars[(bars.dt.dt.date == clock.date()) & (bars.dt <= clock)]
    bars = bars.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
    bars = bars.sort_values('dt').drop_duplicates('dt', keep='last')
    if bars.empty:
        raise ValueError('no current-session candles')
    # Require the newest completed candle, allowing the first 30 seconds for
    # publication. Forming candles alone are not proof of tick freshness.
    expected = (clock - pd.Timedelta(seconds=30)).floor('5min') - pd.Timedelta(minutes=5)
    expected = min(expected, clock.normalize() + pd.Timedelta(hours=15, minutes=25))
    if expected >= clock.normalize() + pd.Timedelta(hours=9, minutes=15) and expected not in set(bars['dt']):
        raise ValueError(f'stale/missing completed candle expected={expected.isoformat()} latest={bars.iloc[-1]["dt"].isoformat()}')
    if bars.iloc[-1]['dt'] < expected:
        raise ValueError(f'stale latest={bars.iloc[-1]["dt"].isoformat()} expected>={expected.isoformat()}')
    return bars


def fetch(symbol, security_id, now):
    """Yahoo only; security_id retained for caller compatibility, never used.

    The shared fetcher handles query1/query2 and authentication refresh just as
    it does for M6. It can return None after failures, so no HTTP status is
    invented here. A one-second polling target cannot guarantee publication.
    """
    try:
        bars = current_bars(feeds.fetch_bars_yahoo(symbol, '1d'), now)
    except ValueError as exc:
        raise ValueError(f'no usable M16 feed: yahoo: {exc}') from exc
    return bars, dict(provider='yahoo', latest=bars.iloc[-1]['dt'].isoformat())
