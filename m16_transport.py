"""Opt-in, M16-process-only Yahoo pacing and cross-process circuit breaker.

No changes to the shared feed implementation or M6 runtime. All M16 sector,
index/history and stock chart callers share a lock-protected local budget.
"""
import fcntl
import functools
import json
import os
from pathlib import Path
import tempfile
import time


def state_path():
    return Path(os.getenv('M16_YAHOO_BUDGET_FILE',
        str(Path(tempfile.gettempdir()) / f'm16-yahoo-{os.getuid()}.json')))


def update(fn):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as handle:
        deadline = time.monotonic()+2.
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('M16 Yahoo budget lock unavailable for 2s')
                time.sleep(.01)
        try:
            handle.seek(0)
            raw = handle.read()
            data = json.loads(raw) if raw else {}
            result = fn(data)
            handle.seek(0)
            handle.truncate()
            json.dump(data, handle)
            handle.flush()
            return result
        finally:
            # close() alone does not release flock if a forked child inherited
            # this open-file description. Explicit unlock is essential.
            fcntl.flock(handle, fcntl.LOCK_UN)



def acquire():
    """Pace requests; return immediately rather than sleep through a cooldown."""
    while True:
        now = time.time()
        def reserve(data):
            cooldown = max(0., data.get('cooldown_until', 0)-now)
            if cooldown:
                return cooldown, True
            wait = max(0., data.get('next', 0)-now)
            if not wait:
                data['requests_started'] = data.get('requests_started', 0)+1
                data['next'] = now + max(.05, float(os.getenv('M16_YAHOO_REQUEST_INTERVAL', '.125')))
            return wait, False
        delay, blocked = update(reserve)
        if blocked:
            return delay
        if not delay:
            return 0.
        time.sleep(min(delay, .25))


def trip(retry_after):
    now = time.time()
    def change(data):
        failures = data.get('strikes', 0)+1 if now-data.get('last_429', 0) < 600 else 1
        delay = max(float(retry_after or 0), min(300, 30 * 2**min(failures-1, 4)))
        data.update(strikes=failures, last_429=now,
                    cooldown_until=max(data.get('cooldown_until', 0), now+delay))
    update(change)


def status():
    if os.getenv('M16_YAHOO_COORDINATE') != '1':
        return dict(enabled=False)
    try:
        return update(lambda data: dict(enabled=True,
            requests_started=data.get('requests_started', 0), strikes=data.get('strikes', 0),
            cooldown_seconds=max(0.,data.get('cooldown_until',0)-time.time())))
    except TimeoutError as exc:
        return dict(enabled=True, error=str(exc))


def install():
    """Only called in M16 executable/sector workers; opt-in via workflow env."""
    if os.getenv('M16_YAHOO_COORDINATE') != '1':
        return
    import feeds
    original = feeds.fetch_bars_yahoo
    if getattr(original, '_m16_coordinated', False) is True:
        return

    @functools.wraps(original)
    def fetch(symbol, *args, **kwargs):
        blocked = acquire()
        callback = kwargs.get('on_error')
        if blocked:
            error = dict(host='shared-m16-budget', reason='shared 429 cooldown',
                         http_status=429, retry_after=blocked)
            if callback:
                callback(error)
            if kwargs.get('raise_errors'):
                raise feeds.YahooFetchError(symbol, error['host'], error['reason'], 429, blocked)
            return None
        def report(error):
            if error.get('http_status') == 429:
                trip(error.get('retry_after'))
            if callback:
                callback(error)
        kwargs['on_error'] = report
        return original(symbol, *args, **kwargs)
    fetch._m16_coordinated = True
    feeds.fetch_bars_yahoo = fetch
