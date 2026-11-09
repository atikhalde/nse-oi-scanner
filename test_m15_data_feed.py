#!/usr/bin/env python3
"""M15 data feed contract — same feeds.py ladder as M1–M6, no yfinance.

These tests are offline: every network leg is monkeypatched, so they pin the
ladder order, the OHLCV contract and the feed audit without touching Yahoo.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parent
IST = "Asia/Kolkata"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bars(start="2026-11-09 10:00", n=3, vol=100.0) -> pd.DataFrame:
    t0 = pd.Timestamp(start, tz=IST)
    return pd.DataFrame(
        {
            "dt": [t0 + pd.Timedelta(minutes=5 * i) for i in range(n)],
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [vol] * n,
        }
    )


def _one_minute_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100.0] * 15,
            "High": [101.0] * 15,
            "Low": [99.0] * 15,
            "Close": [100.5] * 15,
            "Volume": [10.0] * 15,
        },
        index=pd.date_range("2026-11-09 10:00", periods=15, freq="1min", tz=IST),
    )


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def test_data_module_has_no_yfinance():
    """M15 must read bars through feeds.py, not yfinance."""
    import nse_scanner.data as d

    src = (ROOT / "nse_scanner" / "data.py").read_text(encoding="utf-8")
    assert "import yfinance" not in src
    assert "yfinance as yf" not in src
    assert "yf." not in src
    assert not hasattr(d, "yf")
    assert hasattr(d, "feeds") and hasattr(d.feeds, "fetch_bars_yahoo")


def test_yahoo_ticker_mapping():
    import feeds

    assert feeds._yahoo_ticker("RELIANCE") == "RELIANCE.NS"
    assert feeds._yahoo_ticker("M&M") == "M&M.NS"
    assert feeds._yahoo_ticker("BAJAJ-AUTO") == "BAJAJ-AUTO.NS"
    assert feeds._yahoo_ticker("^NSEI") == "^NSEI"
    assert feeds._yahoo_ticker("^INDIAVIX") == "^INDIAVIX"
    assert feeds._yahoo_ticker("NIFTY_FIN_SERVICE.NS") == "NIFTY_FIN_SERVICE.NS"
    assert feeds._yahoo_ticker("RELIANCE.NS") == "RELIANCE.NS"


def test_yahoo_is_primary_and_contract_is_stable():
    import nse_scanner.data as d

    calls: list[tuple] = []

    def fake(symbol, rng="1d", interval="5m"):
        calls.append((symbol, rng, interval))
        return _bars()

    with patch.object(d.feeds, "fetch_bars_yahoo", side_effect=fake):
        df = d.fetch_bars("INFY.NS", "7d", "5m")

    assert calls and calls[0][0] == "INFY.NS"
    assert calls[0][2] == "5m", "M15 intraday must ask feeds for 5m bars"
    assert list(df.columns) == d.COLUMNS
    assert str(df.index.tz) == IST
    assert df.index.is_monotonic_increasing
    assert len(df) == 3


def test_dhan_fallback_only_when_yahoo_misses():
    import nse_scanner.data as d

    dhan_calls = []

    def fake_dhan(security_id, frm, to, interval="5"):
        dhan_calls.append((security_id, frm, to, interval))
        return _bars()

    with patch.object(d.feeds, "fetch_bars_yahoo", return_value=None), \
            patch.object(d.feeds, "dhan_ok", return_value=True), \
            patch.object(d, "security_id", return_value="1594"), \
            patch.object(d.feeds, "fetch_bars_dhan", side_effect=fake_dhan):
        df = d.fetch_bars("INFY", "7d", "5m")

    assert len(df) == 3
    assert dhan_calls and dhan_calls[0][0] == "1594" and dhan_calls[0][3] == "5"

    # No securityId (indices / new listings) => never invent a Dhan call.
    dhan_calls.clear()
    with patch.object(d.feeds, "fetch_bars_yahoo", return_value=None), \
            patch.object(d.feeds, "dhan_ok", return_value=True), \
            patch.object(d, "security_id", return_value=None), \
            patch.object(d.feeds, "fetch_bars_dhan", side_effect=fake_dhan):
        df = d.fetch_bars("^NSEI", "7d", "5m")
    assert df.empty and not dhan_calls


def test_history_is_last_resort_for_5m():
    import nse_scanner.data as d

    with patch.object(d.feeds, "fetch_bars_yahoo", return_value=None), \
            patch.object(d, "security_id", return_value=None):
        df = d.fetch_bars("RELIANCE", "7d", "5m")

    assert not df.empty, "committed data/history must rescue a Yahoo+Dhan miss"
    assert df.index[-1].date() <= dt.date(2026, 11, 6)
    assert str(df.index.tz) == IST


def test_daily_uses_local_history_first():
    import nse_scanner.data as d

    with patch.object(d.feeds, "fetch_bars_yahoo",
                      side_effect=AssertionError("Yahoo must not be called for fresh local daily")), \
            patch.object(d, "now_ist", return_value=dt.datetime(2026, 11, 9, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))):
        daily = d.fetch_bars("RELIANCE", "90d", "1d")

    assert not daily.empty
    assert (daily.index.hour == 9).all() and (daily.index.minute == 15).all()
    assert daily.index.is_monotonic_increasing
    assert daily["High"].ge(daily["Low"]).all()


def test_indices_fall_back_to_yahoo_daily():
    """Nifty/VIX/sector indices have no history CSV -> Yahoo daily."""
    import nse_scanner.data as d

    calls = []

    def fake(symbol, rng="1d", interval="5m"):
        calls.append((symbol, interval))
        return _bars("2026-11-05 09:15", n=10)

    with patch.object(d.feeds, "fetch_bars_yahoo", side_effect=fake):
        df = d.fetch_bars("^NSEI", "90d", "1d")

    assert calls == [("^NSEI", "1d")]
    assert not df.empty


def test_fetch_many_isolates_failures_and_audits_sources():
    import nse_scanner.data as d

    def fake(symbol, rng="1d", interval="5m"):
        d.feeds._FEED_STATS["yahoo_calls"] += 1
        if symbol == "BAD.NS":
            raise RuntimeError("boom")
        return _bars()

    d.reset_feed_stats()
    with patch.object(d.feeds, "fetch_bars_yahoo", side_effect=fake):
        out = d.fetch_many(["INFY.NS", "BAD.NS", "TCS.NS"], "7d", "5m")

    assert set(out) == {"INFY.NS", "BAD.NS", "TCS.NS"}
    assert out["BAD.NS"].empty, "one dead symbol must not abort the batch"
    assert not out["INFY.NS"].empty

    stats = d.feed_stats()
    assert stats["symbols"] == 3
    assert stats["sources"].get("yahoo") == 2
    assert stats["sources"].get("none") == 1
    assert stats["yahoo_calls"] == 3, "the failing attempt is still a Yahoo call"


def test_needs_1m_volume_gate_and_repair():
    import nse_scanner.data as d

    df5 = d._normalize_ohlcv(_bars(n=3))
    assert not d.needs_1m_volume(df5)
    df5.loc[df5.index[0], "Volume"] = 0.0
    assert d.needs_1m_volume(df5)

    fixed = d.apply_1m_volume(df5, _one_minute_frame())
    assert float(fixed["Volume"].iloc[0]) > 0.0
    assert float(fixed["Volume"].iloc[1]) == 100.0, "non-zero volume must be untouched"


def test_interval_and_period_aliases():
    import nse_scanner.data as d

    assert d._canon_interval("5m") == "5m"
    assert d._canon_interval("5") == "5m"
    assert d._canon_interval("1m") == "1m"
    assert d._canon_interval("1d") == "1d"
    assert d._period_days("7d") == 7
    assert d._period_days("90d") == 90
    assert d._period_days("3mo") == 90


def test_security_ids_loaded_from_m1_universe():
    import nse_scanner.data as d

    ids = d.load_security_ids()
    assert ids.get("RELIANCE"), "fno_universe.csv securityId must be available"
    assert d.security_id("RELIANCE.NS") == ids["RELIANCE"]
    assert d.security_id("^NSEI") is None


if __name__ == "__main__":
    test_data_module_has_no_yfinance()
    test_yahoo_ticker_mapping()
    test_yahoo_is_primary_and_contract_is_stable()
    test_dhan_fallback_only_when_yahoo_misses()
    test_history_is_last_resort_for_5m()
    test_daily_uses_local_history_first()
    test_indices_fall_back_to_yahoo_daily()
    test_fetch_many_isolates_failures_and_audits_sources()
    test_needs_1m_volume_gate_and_repair()
    test_interval_and_period_aliases()
    test_security_ids_loaded_from_m1_universe()
    print("test_m15_data_feed OK")
