"""M16 Yahoo-only feed, using M6's Yahoo fetcher with freshness checks."""
import threading
import time
import pandas as pd
import feeds

_COOLDOWN_UNTIL = 0.0
_COOLDOWN_LOCK = threading.Lock()
_LAST_SESSION_RESET = float("-inf")


def _ist(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize('Asia/Kolkata') if ts.tzinfo is None else ts.tz_convert('Asia/Kolkata')


def required_closed_bar(now):
    """Latest completed 5m candle required to consider a current-session feed fresh.

    Allows 30s publication grace after each 5m close and caps at 15:25 IST.
    During NSE's closing-price aggregation window (15:20:30 <= now < 15:30:30),
    Yahoo omits the 15:15 candle on ~35-80% of NSE equities and omits 15:20 on
    ~98% before publishing 15:25 after 15:30 IST; 15:10 remains the latest
    universally published pre-close candle until 15:25 is due at 15:30:30.
    """
    clock = _ist(now)
    day = clock.normalize()
    expected = (clock - pd.Timedelta(seconds=30)).floor('5min') - pd.Timedelta(minutes=5)
    expected = min(expected, day + pd.Timedelta(hours=15, minutes=25))
    closing_gap_start = day + pd.Timedelta(hours=15, minutes=15)
    closing_final = day + pd.Timedelta(hours=15, minutes=25)
    if closing_gap_start <= expected < closing_final:
        return day + pd.Timedelta(hours=15, minutes=10)
    return expected


def current_bars(raw, now):
    if raw is None or raw.empty:
        raise ValueError('empty response')
    bars = raw.copy()
    bars['dt'] = pd.to_datetime(bars['dt'], utc=True).dt.tz_convert('Asia/Kolkata')
    clock = _ist(now)
    bars = bars[(bars.dt.dt.date == clock.date()) & (bars.dt <= clock)]
    # Yahoo may append a last-quote row at e.g. 14:26:13. It is NOT a
    # five-minute candle; do not let it falsely advertise current coverage.
    bars = bars[(bars.dt.dt.minute % 5 == 0) & (bars.dt.dt.second == 0)]
    bars = bars.dropna(subset=['open', 'high', 'low', 'close', 'volume'])
    bars = bars.sort_values('dt').drop_duplicates('dt', keep='last')
    if bars.empty:
        raise ValueError('no current-session candles')
    # Require the newest mandatory completed candle, allowing the first 30s for
    # publication. Forming candles alone are not proof of completed continuity.
    expected = required_closed_bar(clock)
    completed = bars[bars['dt'] + pd.Timedelta(minutes=5) <= clock]
    if expected >= clock.normalize() + pd.Timedelta(hours=9, minutes=15) and expected not in set(completed['dt']):
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
    for window in ('1d', '5d'):
        transport_errors = []
        raw = feeds.fetch_bars_yahoo(symbol, window, on_error=transport_errors.append, timeout=(2, 3))
        try:
            bars = current_bars(raw, now)
            return bars, dict(provider='yahoo', window=window,
                              latest=bars.iloc[-1]['dt'].isoformat(), fallback_errors=errors,
                              transport_errors=transport_errors)
        except ValueError as exc:
            details = dict(window=window, reason=str(exc), transport=transport_errors)
            if raw is not None and not raw.empty and 'dt' in raw:
                details['source_latest'] = str(raw['dt'].iloc[-1])
                details['expected_session'] = str(pd.Timestamp(now).date())
            errors.append(details)
            # A wider date range cannot synthesize unpublished today candles
            # once 1d returned non-empty data without transport errors, nor
            # bypass per-symbol empty-response backoff.
            if (raw is not None and not raw.empty and not transport_errors) or any(
                    x.get('host') == 'shared-m16-symbol' for x in transport_errors):
                break
            if any(x.get('http_status') == 429 for x in transport_errors):
                with _COOLDOWN_LOCK:
                    delays = [float(x.get("retry_after") or 0) for x in transport_errors]
                    minimum = 0. if all(x.get('host') == 'shared-m16-budget' for x in transport_errors) else 30.
                    _COOLDOWN_UNTIL = max(_COOLDOWN_UNTIL, time.monotonic()+max([minimum]+delays))
            # A second range request cannot cure rate limiting/auth failure.
            if any(x.get('http_status') in (401, 403, 429) for x in transport_errors):
                break
    # Empty/stale symbol data is not authentication evidence. The shared
    # fetcher already invalidates and retries on explicit HTTP 401/403.
    raise ValueError(f'no usable M16 feed: yahoo: {errors}')
