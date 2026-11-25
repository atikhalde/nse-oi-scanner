"""M16 feed: throttled Yahoo primary, optional Dhan fallback, strict freshness.

The shared Yahoo fetcher now raises typed HTTP errors. M16 uses those errors to
drive a process-wide circuit breaker and rate limiter, instead of converting a
429/cookie rejection into an endless loop of anonymous "empty response" polls.
"""
from __future__ import annotations

import os
import threading
import time

import pandas as pd

import feeds

IST = "Asia/Kolkata"


class M16FeedError(ValueError):
    """Actionable M16 feed failure; ValueError preserves the legacy caller API."""

    def __init__(self, message, *, category="unknown", status=None, retry_after=None,
                 provider=None, attempts=None, paused=False):
        super().__init__(message)
        self.category = category
        self.status = int(status) if status is not None else None
        self.retry_after = float(retry_after) if retry_after is not None else None
        self.provider = provider
        self.attempts = list(attempts or [])
        self.paused = bool(paused)


def _env_float(name, default, minimum=0.0):
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return float(default)


def _env_int(name, default, minimum=1):
    try:
        return max(minimum, int(float(os.getenv(name, str(default)))))
    except (TypeError, ValueError):
        return int(default)


def current_bars(raw, now):
    if raw is None or raw.empty:
        raise ValueError('empty response')
    bars = raw.copy()
    bars['dt'] = pd.to_datetime(bars['dt'], utc=True).dt.tz_convert(IST)
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


class _FeedBreaker:
    """Shared request pacing and Yahoo outage protection for all M16 threads."""

    def __init__(self):
        self.cond = threading.Condition()
        self.next_slot = 0.0
        self.inflight = 0
        self.cooldown_until = 0.0
        self.prefer_fallback_until = 0.0
        self.dhan_disabled_until = 0.0
        self.consecutive = 0
        self.last_error = None
        self.last_status = None
        self.last_category = None

    @staticmethod
    def _config():
        return (_env_int('M16_YAHOO_MAX_CONCURRENT', 4),
                _env_float('M16_YAHOO_MIN_INTERVAL', 0.25),
                _env_float('M16_YAHOO_BACKOFF_SECONDS', 30),
                _env_float('M16_YAHOO_MAX_BACKOFF_SECONDS', 300),
                _env_int('M16_YAHOO_TRIP_AFTER', 3),
                _env_float('M16_DHAN_PREFER_SECONDS', 120))

    def dhan_available(self, now=None, security_id='*'):
        """`'*'` asks whether Dhan is globally available; None/'' means no SID."""
        now = time.monotonic() if now is None else now
        return bool(security_id and feeds.dhan_ok() and now >= self.dhan_disabled_until)

    def _retry_in(self, now=None):
        now = time.monotonic() if now is None else now
        return max(0.0, self.cooldown_until - now)

    def _skip_all(self, now, fallback_possible):
        # A Yahoo cooldown never blocks the authorized fallback. If no fallback
        # exists, quickly fail instead of accumulating one request per symbol.
        return self._retry_in(now) > 0 and not fallback_possible

    def acquire(self, symbol, fallback_possible=False):
        """Acquire a shared pacing slot; return the real wait to add to `now`."""
        started = time.monotonic()
        now = started
        with self.cond:
            if self._skip_all(now, fallback_possible):
                retry = self._retry_in(now)
                raise M16FeedError(f'M16 feed paused after Yahoo {self.last_category or "failure"}; retry in {retry:.1f}s',
                                   category=self.last_category or 'paused', status=self.last_status,
                                   retry_after=retry, provider='yahoo', paused=True)
            max_concurrent, min_interval, _, _, _, _ = self._config()
            while self.inflight >= max_concurrent or now < self.next_slot:
                wait_for = max(0.01, self.next_slot - now) if now < self.next_slot else 0.5
                self.cond.wait(timeout=min(0.5, wait_for))
                now = time.monotonic()
                if self._skip_all(now, fallback_possible):
                    retry = self._retry_in(now)
                    raise M16FeedError(f'M16 feed paused after Yahoo {self.last_category or "failure"}; retry in {retry:.1f}s',
                                       category=self.last_category or 'paused', status=self.last_status,
                                       retry_after=retry, provider='yahoo', paused=True)
            self.next_slot = max(now, self.next_slot) + min_interval
            self.inflight += 1
            return max(0.0, time.monotonic() - started)

    def release(self):
        with self.cond:
            self.inflight = max(0, self.inflight - 1)
            self.cond.notify_all()

    def should_skip_yahoo(self):
        now = time.monotonic()
        return now < max(self.cooldown_until, self.prefer_fallback_until)

    def success(self):
        with self.cond:
            self.consecutive = 0
            self.cooldown_until = 0.0
            self.prefer_fallback_until = 0.0
            self.last_error = None
            self.last_status = None
            self.last_category = None
            self.cond.notify_all()

    def failure(self, err):
        category = getattr(err, 'category', 'transport')
        status = getattr(err, 'status', None)
        retry_after = getattr(err, 'retry_after', None)
        hard = (status == 429 or status in (401, 403) or
                (status is not None and status >= 500) or
                category in ('rate_limited', 'auth', 'server'))
        with self.cond:
            self.consecutive += 1
            self.last_error = str(err)
            self.last_status = status
            self.last_category = category
            _, _, base, ceiling, trip_after, prefer_seconds = self._config()
            if hard or self.consecutive >= trip_after:
                exponent = min(self.consecutive - 1, 8)
                delay = min(ceiling, max(0.0, base * (2 ** exponent)))
                if retry_after is not None:
                    delay = max(delay, min(ceiling, float(retry_after)))
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + delay)
                self.prefer_fallback_until = max(self.prefer_fallback_until,
                                                 time.monotonic() + max(prefer_seconds, delay))
            self.cond.notify_all()

    def note_dhan_failure(self, status):
        if status in (401, 403, 429):
            with self.cond:
                self.dhan_disabled_until = max(self.dhan_disabled_until,
                                               time.monotonic() + _env_float('M16_DHAN_COOLDOWN_SECONDS', 300))

    def note_fallback_success(self):
        with self.cond:
            self.consecutive = 0
            self.prefer_fallback_until = max(self.prefer_fallback_until,
                                             time.monotonic() + self._config()[5])
            self.dhan_disabled_until = 0.0
            self.cond.notify_all()

    def status(self):
        now = time.monotonic()
        max_concurrent, min_interval, base, ceiling, trip_after, _ = self._config()
        paused = self._retry_in(now) > 0
        return dict(paused=paused, retry_in=self._retry_in(now),
                    skip_all=paused and not self.dhan_available(now),
                    fallback_available=self.dhan_available(now),
                    prefer_fallback=now < self.prefer_fallback_until,
                    consecutive=self.consecutive, last_status=self.last_status,
                    last_category=self.last_category, last_error=self.last_error,
                    inflight=self.inflight, max_concurrent=max_concurrent,
                    min_interval=min_interval, backoff_seconds=base,
                    max_backoff_seconds=ceiling, trip_after=trip_after)

    def reset(self):
        with self.cond:
            self.next_slot = 0.0
            self.inflight = 0
            self.cooldown_until = 0.0
            self.prefer_fallback_until = 0.0
            self.dhan_disabled_until = 0.0
            self.consecutive = 0
            self.last_error = None
            self.last_status = None
            self.last_category = None
            self.cond.notify_all()


_BREAKER = _FeedBreaker()


def breaker_status():
    return _BREAKER.status()


def reset_breaker():
    _BREAKER.reset()


def error_detail(exc):
    """JSON-safe details for the durable feed-health journal."""
    if isinstance(exc, M16FeedError):
        return dict(category=exc.category, status=exc.status, retry_after=exc.retry_after,
                    provider=exc.provider, paused=exc.paused,
                    attempts=exc.attempts, reason=str(exc))
    return dict(category=type(exc).__name__, status=None, retry_after=None,
                provider=None, paused=False, attempts=[], reason=str(exc))


def _coerce_failure(symbol, exc):
    if isinstance(exc, feeds.YahooFetchError):
        return exc
    if isinstance(exc, ValueError):
        err = feeds.YahooFetchError(symbol, 'yahoo', str(exc), 200)
        err.category = 'stale' if 'stale' in str(exc) else 'invalid_data'
        return err
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    return feeds.YahooFetchError(symbol, 'yahoo', f'{type(exc).__name__}: {exc}', status)


def _attempt(err=None, provider='yahoo', ok=False, reason=None):
    return dict(provider=provider, ok=ok, status=getattr(err, 'status', None),
                category=getattr(err, 'category', None if err else 'ok'),
                reason=str(err) if err is not None else reason)


def _try_dhan(symbol, security_id, started, attempts):
    now = time.monotonic()
    if not security_id or not _BREAKER.dhan_available(now, security_id):
        return None
    frm = started.strftime('%Y-%m-%d 09:15:00')
    to = started.strftime('%Y-%m-%d %H:%M:%S')
    try:
        raw = feeds.fetch_bars_dhan(security_id, frm, to, interval='5')
        if raw is None or raw.empty:
            raise ValueError('empty response')
        bars = current_bars(raw, started)
        attempts.append(_attempt(provider='dhan', ok=True, reason='current-session bars accepted'))
        return bars
    except Exception as exc:
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        _BREAKER.note_dhan_failure(status)
        attempts.append(dict(provider='dhan', ok=False, status=status,
                             category='auth' if status in (401, 403) else 'transport',
                             reason=f'{type(exc).__name__}: {exc}'))
        return None


def fetch(symbol, security_id, now):
    """Return current-session bars with a strict request-start watermark.

    Yahoo is paced by a process-wide limiter/circuit breaker. On an outage, a
    configured Dhan token may provide the current bars while Yahoo cools down.
    Every failure carries its provider, HTTP status and retry information.
    """
    fallback_possible = _BREAKER.dhan_available(time.monotonic(), security_id)
    waited = _BREAKER.acquire(symbol, fallback_possible)
    started = pd.Timestamp(now) + pd.Timedelta(seconds=waited)
    attempts = []
    yahoo_error = None
    try:
        if _BREAKER.should_skip_yahoo():
            status = _BREAKER.status()
            attempts.append(dict(provider='yahoo', ok=False, status=status['last_status'],
                                 category='paused', reason=f"Yahoo cooling down for {status['retry_in']:.1f}s"))
            yahoo_error = feeds.YahooFetchError(symbol, 'yahoo', 'circuit breaker open',
                                                status['last_status'], status['retry_in'])
            yahoo_error.category = 'paused'
        else:
            try:
                raw = feeds.fetch_bars_yahoo(symbol, '1d', raise_errors=True)
                if raw is None or raw.empty:
                    raise feeds.YahooFetchError(symbol, 'yahoo', 'empty response', 200)
                bars = current_bars(raw, started)
                attempts.append(_attempt(provider='yahoo', ok=True, reason='current-session bars accepted'))
                _BREAKER.success()
                return bars, dict(provider='yahoo', latest=bars.iloc[-1]['dt'].isoformat(),
                                  request_started=started.isoformat(), attempts=attempts,
                                  breaker=breaker_status())
            except Exception as exc:
                yahoo_error = _coerce_failure(symbol, exc)
                attempts.append(_attempt(yahoo_error, provider='yahoo'))
                _BREAKER.failure(yahoo_error)

        dhan_bars = _try_dhan(symbol, security_id, started, attempts)
        if dhan_bars is not None:
            _BREAKER.note_fallback_success()
            return dhan_bars, dict(provider='dhan', latest=dhan_bars.iloc[-1]['dt'].isoformat(),
                                   request_started=started.isoformat(), attempts=attempts,
                                   breaker=breaker_status())

        status = _BREAKER.status()
        paused = bool(status['skip_all'])
        if yahoo_error is not None:
            reason = f'no usable M16 feed: yahoo: {yahoo_error.reason}'
            if any(a.get('provider') == 'dhan' for a in attempts):
                reason += '; dhan: unavailable or stale'
        else:
            reason = 'no usable M16 feed: yahoo circuit breaker open'
        raise M16FeedError(reason, category=getattr(yahoo_error, 'category', 'paused'),
                           status=getattr(yahoo_error, 'status', status['last_status']),
                           retry_after=getattr(yahoo_error, 'retry_after', None) or status['retry_in'],
                           provider=(attempts[-1]['provider'] if attempts else 'yahoo'),
                           attempts=attempts, paused=paused)
    finally:
        _BREAKER.release()
