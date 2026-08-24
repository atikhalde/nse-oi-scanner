"""Market-data feeds: Dhan (preferred, real-time) -> Yahoo (free, ~15min delayed).

Dataframe contract: columns [dt (tz Asia/Kolkata), open, high, low, close, volume], 5-min bars.
"""
import os
import requests
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
IST = "Asia/Kolkata"


def dhan_ok():
    return bool(os.environ.get("DHAN_TOKEN"))


def _dh_post(payload):
    tok = os.environ["DHAN_TOKEN"]
    r = requests.post("https://api.dhan.co/v2/charts/intraday", json=payload,
                      headers={"Content-Type": "application/json", "access-token": tok}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if not j.get("timestamp"):
        return None
    df = pd.DataFrame({"dt": pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert(IST),
                       "open": j["open"], "high": j["high"], "low": j["low"],
                       "close": j["close"], "volume": j["volume"]}).dropna()
    return df


def fetch_bars_dhan(security_id, frm, to):
    """frm/to: 'YYYY-MM-DD HH:MM:SS' strings."""
    return _dh_post({"securityId": str(security_id), "exchangeSegment": "NSE_EQ",
                     "instrument": "EQUITY", "interval": "5", "oi": False,
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


def _yahoo_session():
    """Session with Yahoo cookie + crumb (required for equity .NS charts).

    Bare v8 chart URLs return 401/404 for equities since Yahoo's auth change:
    GET fc.yahoo.com to pick up the consent cookie (it returns an error status
    on purpose — only the cookie matters), then GET /v1/test/getcrumb with it.
    Cached module-wide so a 210-symbol cycle costs one cookie/crumb handshake.
    """
    global _YH_SESSION
    if _YH_SESSION is not None:
        return _YH_SESSION
    sess = requests.Session()
    sess.headers.update(UA)
    crumb = None
    try:
        sess.get("https://fc.yahoo.com", timeout=15)  # cookie only; status is irrelevant
    except Exception:
        pass
    try:
        r = sess.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15)
        if r.ok and r.text and not r.text.lstrip().startswith("<"):
            crumb = r.text.strip()
    except Exception:
        pass
    _YH_SESSION = (sess, crumb)
    return _YH_SESSION


def fetch_bars_yahoo(symbol, rng="1d"):
    # Crumb + query1/query2. Bare v8 URLs often return HTTP 404 after Yahoo auth changes.
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            sess, crumb = _yahoo_session()
            url = f"https://{host}/v8/finance/chart/{symbol}.NS?interval=5m&range={rng}"
            if crumb:
                url += f"&crumb={requests.utils.quote(crumb)}"
            r = sess.get(url, timeout=25)
            if r.status_code in (401, 403):
                # Crumb/cookie rejected — force a fresh handshake for the next try.
                print(f"yahoo {symbol}: HTTP {r.status_code} on {host}; re-handshaking")
                globals()["_YH_SESSION"] = None
                continue
            if r.status_code == 404:
                print(f"yahoo {symbol}: HTTP 404 on {host}; trying next host")
                continue
            r.raise_for_status()
            j = r.json()["chart"]["result"][0]
            ts = j.get("timestamp")
            if not ts:
                return None
            q = j["indicators"]["quote"][0]
            df = pd.DataFrame({"dt": pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST),
                               "open": q["open"], "high": q["high"], "low": q["low"],
                               "close": q["close"], "volume": q.get("volume")}).dropna()
            return df
        except Exception as e:
            # Include the exception type: a silent bare message hid a NameError
            # in _yahoo_session for days while every symbol returned None.
            print(f"yahoo {symbol}: {type(e).__name__}: {e}; retrying (no wait)")
    return None


def fetch_today(symbol, security_id, now_ist):
    """Today's 5-min bars (09:15 .. now). Ladder: Dhan -> Yahoo."""
    src = "none"
    frm = now_ist.strftime("%Y-%m-%d 09:15:00")
    to = now_ist.strftime("%Y-%m-%d %H:%M:%S")
    df = None
    if dhan_ok():
        try:
            df = fetch_bars_dhan(security_id, frm, to)
            src = "dhan"
        except Exception as e:
            print(f"dhan {symbol} fail: {e}")
    if df is None or df.empty:
        df = fetch_bars_yahoo(symbol, "1d")
        src = "yahoo-delayed"
    return df, src
