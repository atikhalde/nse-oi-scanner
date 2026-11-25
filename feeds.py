"""Market-data feeds: Yahoo (primary, free, ~15min delayed) -> Dhan (secondary, real-time).

Every runner uses the same ladder: Yahoo first, Dhan only as a fallback when
Yahoo returns nothing for a symbol. Dataframe contract: columns
[dt (tz Asia/Kolkata), open, high, low, close, volume].

`fetch_bars_yahoo()` is interval-aware ("5m" default, also "1m"/"1d") and
accepts plain NSE codes ("RELIANCE"), Yahoo tickers ("RELIANCE.NS") and index
symbols ("^NSEI", "NIFTY_FIN_SERVICE.NS"). This is the single network path for
every model — M1–M6 and M15 (`nse_scanner.data`) all read bars through here.
"""
import os
import threading
import requests
from requests.adapters import HTTPAdapter
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
IST = "Asia/Kolkata"
DHAN_TIMEOUT = (4, 10)
YAHOO_TIMEOUT = (4, 12)
POOL_SIZE = 24


def dhan_ok():
    return bool(os.environ.get("DHAN_TOKEN"))


def _dh_post(payload, timeout=DHAN_TIMEOUT):
    tok = os.environ["DHAN_TOKEN"]
    r = requests.post("https://api.dhan.co/v2/charts/intraday", json=payload,
                      headers={"Content-Type": "application/json", "access-token": tok}, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not j.get("timestamp"):
        return None
    df = pd.DataFrame({"dt": pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(IST),
                       "open": j["open"], "high": j["high"], "low": j["low"],
                       "close": j["close"], "volume": j["volume"]}).dropna()
    return df


def fetch_bars_dhan(security_id, frm, to, interval="5"):
    """Dhan intraday bars. frm/to: 'YYYY-MM-DD HH:MM:SS' strings.

    interval: "1", "5" (minutes) — the same payload shape M1–M6 use.
    """
    with _STATS_LOCK:
        _FEED_STATS["dhan_calls"] += 1
    return _dh_post({"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                     "instrument": "EQUITY", "interval": str(interval), "oi": False,
                     "fromDate": frm, "toDate": to})


def fetch_fut_oi_dhan(security_id, frm, to):
    """Intraday chart incl. OI for a FUTSTK contract id."""
    p = {"securityId": str(security_id), "exchangeSegment": "NSE_FNO",
         "instrument": "FUTSTK", "interval": "5", "oi": True, "fromDate": frm, "toDate": to}
    tok = os.environ["DHAN_TOKEN"]
    r = requests.post("https://api.dhan.co/v2/charts/intraday", json=p,
                      headers={"Content-Type": "application/json", "access-token": tok}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if not j.get("timestamp"):
        return None
    return pd.DataFrame({"dt": pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(IST),
                         "close": j["close"],
                         "oi": j.get("open_interest", [None] * len(j["timestamp"]))})


_YH_SESSION = None  # cached (requests.Session, crumb-or-None)
_YH_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()
_DHAN_AUTH_BAD = False  # tripped on HTTP 401/403 so parallel fallback never repeats bad-token calls

# Per-cycle feed counters (reset by runners via reset_feed_stats() before each
# symbol loop). M12/M13/M14/M6/M15 read these to keep their st["feed"] audit record.
_FEED_STATS = {"yahoo_calls": 0, "dhan_calls": 0, "fallback": None, "last_source": None}


def reset_feed_stats():
    global _DHAN_AUTH_BAD
    with _STATS_LOCK:
        _FEED_STATS.update({"yahoo_calls": 0, "dhan_calls": 0, "fallback": None, "last_source": None})
        _DHAN_AUTH_BAD = False


def feed_stats():
    with _STATS_LOCK:
        return dict(_FEED_STATS)


def _yahoo_session():
    """Session with Yahoo cookie + crumb (required for equity .NS charts).

    Bare v8 chart URLs return 401/404 for equities since Yahoo's auth change:
    GET fc.yahoo.com to pick up the consent cookie (it returns an error status
    on purpose — only the cookie matters), then GET /v1/test/getcrumb with it.
    Cached module-wide with double-checked locking and a widened keep-alive pool
    so both sequential and concurrent cycles share one handshake cleanly.
    """
    global _YH_SESSION
    cached = _YH_SESSION
    if cached is not None:
        return cached
    with _YH_LOCK:
        if _YH_SESSION is not None:
            return _YH_SESSION
        sess = requests.Session()
        if hasattr(sess, "mount"):
            adapter = HTTPAdapter(pool_connections=POOL_SIZE, pool_maxsize=POOL_SIZE)
            sess.mount("https://", adapter)
            sess.mount("http://", adapter)
        sess.headers.update(UA)
        crumb = None
        try:
            sess.get("https://fc.yahoo.com", timeout=YAHOO_TIMEOUT)  # cookie only; status is irrelevant
        except Exception:
            pass
        try:
            r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=YAHOO_TIMEOUT)
            if r.ok and r.text and not r.text.lstrip().startswith("<"):
                crumb = r.text.strip()
        except Exception:
            pass
        _YH_SESSION = (sess, crumb)
        return _YH_SESSION


def warm_yahoo_session():
    """Pre-warm Yahoo cookie/crumb and keep-alive TLS socket before candle close."""
    try:
        sess, crumb = _yahoo_session()
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=5m&range=1d"
        if crumb:
            url += f"&crumb={requests.utils.quote(crumb)}"
        r = sess.get(url, timeout=YAHOO_TIMEOUT)
        if r.status_code in (401, 403):
            with _YH_LOCK:
                if _YH_SESSION is not None and _YH_SESSION[0] is sess:
                    globals()["_YH_SESSION"] = None
            _yahoo_session()
    except Exception:
        pass


def _yahoo_ticker(symbol: str) -> str:
    """Map an NSE code / Yahoo ticker to a Yahoo chart path segment.

    "^NSEI" and already-suffixed "NIFTY_FIN_SERVICE.NS" stay untouched; a plain
    NSE code ("RELIANCE", "M&M", "BAJAJ-AUTO") gains the ".NS" suffix.
    """
    s = str(symbol).strip()
    if not s:
        return s
    if s.startswith("^") or s.upper().endswith((".NS", ".BO")):
        return s
    return f"{s}.NS"


def fetch_bars_yahoo(symbol, rng="1d", interval="5m", on_error=None, timeout=None):
    """Yahoo chart bars via the shared cookie/crumb session.

    interval: "5m" (default, M1–M6 live path), "1m" (09:15 volume repair) or
    "1d" (daily context). Returns [dt, open, high, low, close, volume] or None.
    """
    def report(host, reason, status=None):
        if on_error is not None:
            on_error(dict(host=host, reason=reason, http_status=status))

    ticker = _yahoo_ticker(symbol)
    with _STATS_LOCK:
        _FEED_STATS["yahoo_calls"] += 1
    # Crumb + query1/query2. Bare v8 URLs often return HTTP 404 after Yahoo auth changes.
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        sess = None
        try:
            sess, crumb = _yahoo_session()
            url = f"https://{host}/v8/finance/chart/{ticker}?interval={interval}&range={rng}"
            if crumb:
                url += f"&crumb={requests.utils.quote(crumb)}"
            r = sess.get(url, timeout=timeout or YAHOO_TIMEOUT)
            if r.status_code >= 400:
                report(host, "HTTP error", r.status_code)
            if r.status_code in (401, 403):
                # Crumb/cookie rejected — invalidate this session once and retry.
                print(f"yahoo {symbol}: HTTP {r.status_code} on {host}; re-handshaking")
                with _YH_LOCK:
                    if _YH_SESSION is not None and _YH_SESSION[0] is sess:
                        globals()["_YH_SESSION"] = None
                continue
            if r.status_code == 404:
                print(f"yahoo {symbol}: HTTP 404 on {host}; trying next host")
                continue
            r.raise_for_status()
            j = r.json()["chart"]["result"][0]
            ts = j.get("timestamp")
            if not ts:
                report(host, "no chart timestamps")
                return None
            q = j["indicators"]["quote"][0]
            vol = q.get("volume")
            if vol is None:
                vol = [0] * len(ts)
            df = pd.DataFrame({"dt": pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST),
                               "open": q["open"], "high": q["high"], "low": q["low"],
                               "close": q["close"], "volume": vol})
            df = df.dropna(subset=["open", "high", "low", "close"])
            if not df.empty:
                df["volume"] = df["volume"].fillna(0)
                with _STATS_LOCK:
                    _FEED_STATS["last_source"] = "yahoo"
            return df
        except Exception as e:
            # Include the exception type: a silent bare message hid a NameError
            # in _yahoo_session for days while every symbol returned None.
            report(host, type(e).__name__)
            print(f"yahoo {symbol}: {type(e).__name__}: {e}; retrying (no wait)")
    return None


def fetch_today(symbol, security_id, now_ist):
    """Today's 5-min bars (09:15 .. now). Ladder: Yahoo (primary) -> Dhan (fallback).

    One Yahoo miss never kills the cycle: the symbol falls back to Dhan when a
    token is configured. feed_stats() counters are maintained inside
    fetch_bars_yahoo()/fetch_bars_dhan() so every caller shares one audit.
    """
    global _DHAN_AUTH_BAD
    src = "none"
    df = fetch_bars_yahoo(symbol, "1d")
    if df is not None and not df.empty:
        return df, "yahoo"
    if dhan_ok() and not _DHAN_AUTH_BAD:
        with _STATS_LOCK:
            if _FEED_STATS["fallback"] is None:
                _FEED_STATS["fallback"] = "yahoo-miss"
        frm = now_ist.strftime("%Y-%m-%d 09:15:00")
        to = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        try:
            df = fetch_bars_dhan(security_id, frm, to)
            src = "dhan"
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (401, 403):
                with _STATS_LOCK:
                    if not _DHAN_AUTH_BAD:
                        _DHAN_AUTH_BAD = True
                        print(f"dhan fallback {symbol}: HTTP {status} (token invalid/expired — skipping Dhan for remainder of cycle)")
            else:
                print(f"dhan fallback {symbol} fail: {e}")
    if src != "none" and df is not None and not df.empty:
        with _STATS_LOCK:
            _FEED_STATS["last_source"] = src
    return df, src
