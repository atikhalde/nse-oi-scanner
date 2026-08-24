#!/usr/bin/env python3
import datetime as dt, os
import pandas as pd
import fast_feed as F

def ok(n, c):
    assert c, n
    print("PASS", n)

class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._p = payload or {}
        self.content = b""
    def raise_for_status(self):
        if self.status_code >= 400:
            raise F.requests.HTTPError(f"HTTP {self.status_code}", response=self)
    def json(self):
        return self._p

def yahoo_df():
    return pd.DataFrame({
        "dt": [pd.Timestamp("2026-08-11 10:00", tz="Asia/Kolkata")],
        "open": [100], "high": [101], "low": [99], "close": [100.5], "volume": [1000],
    })

def test_yahoo_session_contract():
    """Regression: a missing _yahoo_session NameError was swallowed by the
    broad except inside feeds.fetch_bars_yahoo, silently returning None for
    every symbol (2026-08-24 outage). Pin the contract with mocked transport:
    the helper must exist, hand shake once per cycle, attach the crumb, and
    recover from a 401 by re-handshaking."""
    import feeds

    class Resp:
        def __init__(self, status, text=""):
            self.status_code = status; self.text = text; self.ok = status < 400
            self.headers = {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise F.requests.HTTPError(f"HTTP {self.status_code}", response=self)
        def json(self):
            return {"chart": {"result": [{"timestamp": [1756102500],
                "indicators": {"quote": [{"open": [1.0], "high": [2.0], "low": [1.0],
                                          "close": [1.5], "volume": [9]}]}}]}}

    urls = []
    class Sess:
        def __init__(self, chart_status=200):
            self.headers = {}; self.chart_status = chart_status
        def get(self, url, **kw):
            urls.append(url)
            if url.startswith("https://fc.yahoo.com"): return Resp(404)
            if url.endswith("/v1/test/getcrumb"): return Resp(200, "CRUMB1")
            return Resp(self.chart_status)

    old_sess, old_cache = feeds.requests.Session, feeds._YH_SESSION
    try:
        feeds.requests.Session = Sess
        feeds._YH_SESSION = None
        sess, crumb = feeds._yahoo_session()
        ok("_yahoo_session exists and returns a crumb", crumb == "CRUMB1")
        n = len(urls)
        feeds._yahoo_session()
        ok("session cached: one handshake per cycle", len(urls) == n)
        urls.clear()
        df = feeds.fetch_bars_yahoo("RELIANCE", "1d")
        ok("chart URL carries the crumb", df is not None and len(df) == 1
           and any("crumb=CRUMB1" in u for u in urls))
        feeds._YH_SESSION = (Sess(chart_status=401), "STALE")
        df = feeds.fetch_bars_yahoo("RELIANCE", "1d")
        ok("HTTP 401 discards poisoned session, re-handshakes, recovers",
           df is not None and len(df) == 1 and feeds._YH_SESSION[1] == "CRUMB1")
    finally:
        feeds.requests.Session, feeds._YH_SESSION = old_sess, old_cache


def main():
    oldtok = os.environ.get("DHAN_TOKEN")
    oldpost = F.requests.post
    oldyahoo = F.feeds.fetch_bars_yahoo
    calls = {"post": 0, "get": 0}
    test_yahoo_session_contract()
    try:
        os.environ["DHAN_TOKEN"] = "dummy"
        def post(*a, **k):
            calls["post"] += 1
            return Resp(429)
        def yfetch(*a, **k):
            calls["get"] += 1
            return yahoo_df()
        F.requests.post = post
        F.feeds.fetch_bars_yahoo = yfetch
        c = F.FastFeedCycle()
        now = dt.datetime(2026, 8, 11, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
        d, s = c.fetch("AAA", 1, now)
        ok("first Dhan 429 falls through immediately to Yahoo",
           s == "yahoo-fast" and len(d) == 1 and calls == {"post": 1, "get": 1})
        d, s = c.fetch("BBB", 2, now)
        ok("circuit breaker skips Dhan for every remaining symbol",
           s == "yahoo-fast" and calls == {"post": 1, "get": 2})
        ok("429 reason logged once per cycle", c.trip_reason == "HTTP 429")

        def badfetch(*a, **k):
            calls["get"] += 1
            raise F.requests.Timeout("x")
        F.feeds.fetch_bars_yahoo = badfetch
        before = calls["get"]
        d, s = c.fetch("CCC", 3, now)
        ok("Yahoo failure has no retry or sleep", d is None and s == "none" and calls["get"] == before + 1)
    finally:
        F.requests.post = oldpost
        F.feeds.fetch_bars_yahoo = oldyahoo
        if oldtok is None:
            os.environ.pop("DHAN_TOKEN", None)
        else:
            os.environ["DHAN_TOKEN"] = oldtok
    print("ALL FAST FEED TESTS PASSED")

if __name__ == "__main__":
    main()
