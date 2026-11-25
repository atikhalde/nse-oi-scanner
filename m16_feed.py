"""M16 Yahoo-only feed, using M6's Yahoo fetcher with freshness checks."""
import threading
import time
import pandas as pd
import feeds

_COOLDOWN_UNTIL = 0.0
_COOLDOWN_LOCK = threading.Lock()
_LAST_SESSION_RESET = float("-inf")


def current_bars(raw, now):
    if raw is None or raw.empty:
        raise ValueError('empty response')
    bars = raw.copy()
    bars['dt'] = pd.to_datetime(bars['dt'], utc=True).dt.tz_convert('Asia/Kolkata')
    clock = pd.Timestamp(now)
    bars = bars[(bars.dt.dt.date == clock.date()) & (bars.dt <= clock)]
    # Yahoo may append a last-quote row at e.g. 14:26:13. It is NOT a
    # five-minute candle; do not let it falsely advertise current coverage.
    bars = bars[(bars.dt.dt.minute % 5 == 0) & (bars.dt.dt.second == 0)]
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


def recover_yahoo_session(now):
    """A poisoned cached cookie/session must not require a workflow restart.

    M16 runs in its own process. Reset at most once per two minutes, never
    hold up workers waiting for a concurrent Yahoo authentication handshake.
    """
    global _LAST_SESSION_RESET
    if pd.Timestamp(now).strftime('%H:%M') < '09:21':
        return False
    with _COOLDOWN_LOCK:
        if time.monotonic()-_LAST_SESSION_RESET < 120:
            return False
        if not feeds._YH_LOCK.acquire(blocking=False):
            return False
        try:
            feeds._YH_SESSION = None
            _LAST_SESSION_RESET = time.monotonic()
            return True
        finally:
            feeds._YH_LOCK.release()


def fetch(symbol, security_id, now):
    """Yahoo only; security_id retained for caller compatibility, never used.

    The shared fetcher handles query1/query2 and authentication refresh just as
    it does for M6. It can return None after failures, so no HTTP status is
    invented here. A one-second polling target cannot guarantee publication.
    """
    global _COOLDOWN_UNTIL
    with _COOLDOWN_LOCK:
        if time.monotonic() < _COOLDOWN_UNTIL:
            raise ValueError('no usable M16 feed: yahoo: HTTP 429 shared cooldown')
    errors = []
    empty_response = False
    rate_limited = False
    for window in ('1d', '5d'):
        transport_errors = []
        raw = feeds.fetch_bars_yahoo(symbol, window, on_error=transport_errors.append, timeout=(2, 3))
        empty_response = empty_response or raw is None or raw.empty
        try:
            bars = current_bars(raw, now)
            return bars, dict(provider='yahoo', window=window,
                              latest=bars.iloc[-1]['dt'].isoformat(), fallback_errors=errors,
                              transport_errors=transport_errors)
        except ValueError as exc:
            errors.append(dict(window=window, reason=str(exc), transport=transport_errors))
            if any(x.get('http_status') == 429 for x in transport_errors):
                rate_limited = True
                with _COOLDOWN_LOCK:
                    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic()+max([30.0]+[float(x.get("retry_after") or 0) for x in transport_errors]))
            # A second range request cannot cure rate limiting/auth failure.
            if any(x.get('http_status') in (401, 403, 429) for x in transport_errors):
                break
    if empty_response and not rate_limited and recover_yahoo_session(now):
        errors.append(dict(recovery='cached Yahoo session invalidated; next request reauthenticates'))
    raise ValueError(f'no usable M16 feed: yahoo: {errors}')
