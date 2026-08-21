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

def main():
    oldtok = os.environ.get("DHAN_TOKEN")
    oldpost = F.requests.post
    oldyahoo = F.feeds.fetch_bars_yahoo
    calls = {"post": 0, "get": 0}
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
