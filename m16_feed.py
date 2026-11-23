"""M16-only freshness-aware provider selection; other models are unchanged."""
import threading
import time
import pandas as pd
import feeds

_COOLDOWN = {}
_LOCK = threading.Lock()


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
    """Prefer Dhan when configured; validate every response before fallback.

    Errors contain provider/status context but never response bodies or tokens.
    """
    errors = []
    providers = []
    if feeds.dhan_ok() and security_id:
        providers.append(('dhan', lambda: feeds.fetch_bars_dhan(
            security_id, now.strftime('%Y-%m-%d 09:15:00'), now.strftime('%Y-%m-%d %H:%M:%S'))))
    providers.append(('yahoo', lambda: feeds.fetch_bars_yahoo(symbol, '1d')))
    for name, request in providers:
        with _LOCK:
            blocked = _COOLDOWN.get(name, 0) > time.monotonic()
        if blocked:
            errors.append(dict(provider=name, reason='provider cooldown after auth/rate limit'))
            continue
        try:
            bars = current_bars(request(), now)
            return bars, dict(provider=name, latest=bars.iloc[-1]['dt'].isoformat(), fallback_errors=errors)
        except Exception as exc:
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (401, 403, 429):
                with _LOCK:
                    _COOLDOWN[name] = time.monotonic() + (60 if status == 429 else 300)
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            errors.append(dict(provider=name, reason=reason, http_status=status))
    raise ValueError(f'no usable M16 feed: {errors}')
