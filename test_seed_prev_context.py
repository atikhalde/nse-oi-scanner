"""Offline unit tests for seed_prev_context (no network — fetch is injected)."""
import json
import os
import unittest.mock
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import seed_prev_context as SP


def bars(day: str, bars_spec: list[tuple[str, float, float, float]]) -> pd.DataFrame:
    """bars_spec: [(hhmm, high, low, close), ...] for a single session."""
    return pd.DataFrame({
        "dt": pd.to_datetime([f"{day} {t}+05:30" for t, _, _, _ in bars_spec]),
        "open": [c for _, _, _, c in bars_spec],
        "high": [h for _, h, _, _ in bars_spec],
        "low": [l for _, _, l, _ in bars_spec],
        "close": [c for _, _, _, c in bars_spec],
        "volume": [1000.0] * len(bars_spec),
    })


class TestSessionOHLC(unittest.TestCase):
    def test_full_session_close_and_pivot(self):
        df = bars("2026-08-28", [
            ("09:15", 101.0, 99.0, 100.0),
            ("15:15", 102.0, 100.0, 101.0),
            ("15:25", 103.5, 102.0, 103.0),  # official close bar
        ])
        ohlc = SP.session_ohlc(df, "2026-08-28")
        self.assertEqual(ohlc["close"], 103.0)
        self.assertEqual(ohlc["bar_min"], "15:25")
        self.assertAlmostEqual(ohlc["pivot"], (103.5 + 99.0 + 103.0) / 3.0)

    def test_ignores_other_days(self):
        df = pd.concat([
            bars("2026-08-27", [("15:25", 90.0, 89.0, 89.5)]),
            bars("2026-08-28", [("15:25", 100.0, 99.0, 99.5)]),
        ], ignore_index=True)
        ohlc = SP.session_ohlc(df, "2026-08-28")
        self.assertEqual(ohlc["close"], 99.5)

    def test_bad_input_returns_none(self):
        self.assertIsNone(SP.session_ohlc(pd.DataFrame(), "2026-08-28"))
        self.assertIsNone(SP.session_ohlc(None, "2026-08-28"))


class TestDailyPrevContext(unittest.TestCase):
    def test_min_bar_quality_floor(self):
        good = bars("2026-08-28", [("15:25", 100.0, 99.0, 99.5)])
        lagged = bars("2026-08-28", [("15:15", 100.0, 99.0, 99.5)])  # not official

        def fetch(sym, rng):
            return good if sym == "AAA" else lagged

        ctx = SP.daily_prev_context(["AAA", "BBB"], "2026-08-28", fetch=fetch,
                                    sleep_s=0.0, deadline_s=5)
        self.assertEqual(ctx["count"], 1)
        self.assertIn("AAA", ctx["close"])
        self.assertNotIn("BBB", ctx["close"])

    def test_min_bar_none_accepts_final_bar(self):
        lagged = bars("2026-08-28", [("15:15", 100.0, 99.0, 99.5)])
        ctx = SP.daily_prev_context(["BBB"], "2026-08-28",
                                    fetch=lambda s, r: lagged,
                                    min_bar=None, sleep_s=0.0, deadline_s=5)
        self.assertEqual(ctx["count"], 1)

    def test_fetch_errors_are_skipped(self):
        def fetch(sym, rng):
            raise RuntimeError("boom")

        ctx = SP.daily_prev_context(["AAA"], "2026-08-28", fetch=fetch,
                                    sleep_s=0.0, deadline_s=5)
        self.assertEqual(ctx["count"], 0)
        self.assertEqual(ctx["tried"], 1)

    def test_deadline_stops_early(self):
        def fetch(sym, rng):
            return bars("2026-08-28", [("15:25", 100.0, 99.0, 99.5)])

        ctx = SP.daily_prev_context(["A", "B", "C"], "2026-08-28", fetch=fetch,
                                    sleep_s=0.0, deadline_s=0.0)
        self.assertEqual(ctx["tried"], 0)
        self.assertEqual(ctx["count"], 0)


class TestTopUp(unittest.TestCase):
    def test_only_missing_symbols_fetched(self):
        seen = []

        def fetch(sym, rng):
            seen.append(sym)
            return bars("2026-08-28", [("15:25", 100.0, 99.0, 99.5)])

        vals = {"AAA": 100.0}
        piv = {"AAA": 100.5}
        vals, piv, added = SP.top_up(vals, piv, "2026-08-28",
                                     ["AAA", "BBB", "CCC"], fetch=fetch,
                                     sleep_s=0.0, deadline_s=5)
        self.assertEqual(added, 2)
        self.assertEqual(seen, ["BBB", "CCC"])
        self.assertEqual(set(vals), {"AAA", "BBB", "CCC"})


class TestWriteCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self._orig = dict(SP.CACHE_FILES)
        SP.CACHE_FILES = {m: self.tmp_path / f"{m}.json" for m in SP.CACHE_FILES}

    def tearDown(self):
        SP.CACHE_FILES = self._orig

    def _closes(self, n: int) -> dict:
        return {f"S{i:03d}": 100.0 + i for i in range(n)}

    def test_writes_when_sufficient(self):
        meta = SP.write_cache("m12", "2026-08-28", self._closes(200),
                              self._closes(200), "15:25", 210, "daily history seed")
        self.assertEqual(meta["status"], "OK")
        j = json.loads(SP.CACHE_FILES["m12"].read_text())
        self.assertEqual(j["date"], "2026-08-28")
        self.assertEqual(j["count"], 200)
        self.assertEqual(j["close"]["S000"], 100.0)
        self.assertIn("pivot", j)

    def test_partial_never_written(self):
        SP.CACHE_FILES["m12"].write_text('{"date":"old","close":{}}')
        meta = SP.write_cache("m12", "2026-08-28", self._closes(3),
                              self._closes(3), "15:25", 210, "daily history seed")
        self.assertEqual(meta["status"], "INSUFFICIENT")
        j = json.loads(SP.CACHE_FILES["m12"].read_text())
        self.assertEqual(j["date"], "old")  # untouched


class TestTopUpKillSwitchAndRunnerIntegration(unittest.TestCase):
    def test_env_kill_switch_disables_top_up(self):
        import m12_runner as R

        def mk(mins):
            n = len(mins)
            return pd.DataFrame({
                "dt": pd.to_datetime([f"2026-08-29 {t}:00+05:30" for t in mins]),
                "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                "close": [100.5] * n, "volume": [1] * n, "t": mins})

        def explode(symbols, day, **kw):
            raise AssertionError("network top-up must not run with the kill switch on")

        orig = SP.daily_prev_context
        SP.daily_prev_context = explode
        old_cache = None
        import m12_runner
        old_cache = m12_runner.PREV_CACHE
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            m12_runner.PREV_CACHE = Path(td) / "m12_prev_close.json"
            try:
                with unittest.mock.patch.dict(os.environ, {"SEED_PREV_DAILY_TOPUP": "0"}):
                    meta = m12_runner.seed_previous_close_cache(
                        "2026-08-29", {"AAA": mk(["09:20", "09:25"])})
                self.assertEqual(meta["status"], "INSUFFICIENT")
                self.assertFalse(m12_runner.PREV_CACHE.exists())
            finally:
                m12_runner.PREV_CACHE = old_cache
                SP.daily_prev_context = orig

    def test_runner_seed_tops_up_to_complete(self):
        """A 3-symbol intraday map + stubbed daily fetch must produce a full cache."""
        import m12_runner

        def mk(mins):
            n = len(mins)
            return pd.DataFrame({
                "dt": pd.to_datetime([f"2026-08-29 {t}:00+05:30" for t in mins]),
                "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n,
                "close": [100.5] * n, "volume": [1] * n, "t": mins})

        import live_runner as L
        syms = list(L.SYMS)

        def fake_daily(symbols, day, **kw):
            return {"date": str(day), "close": {s: 99.5 for s in symbols},
                    "pivot": {s: 100.0 for s in symbols},
                    "count": len(list(symbols)), "tried": len(list(symbols)),
                    "last_bar_min": "15:25"}

        orig = SP.daily_prev_context
        old_cache = m12_runner.PREV_CACHE
        SP.daily_prev_context = fake_daily
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            m12_runner.PREV_CACHE = Path(td) / "m12_prev_close.json"
            try:
                meta = m12_runner.seed_previous_close_cache(
                    "2026-08-29", {"AAA": mk(["09:20", "09:25"])})
                self.assertEqual(meta["status"], "OK")
                self.assertGreaterEqual(meta["count"], 180)
                self.assertGreaterEqual(meta.get("topped_up", 0), 180)
                self.assertTrue(m12_runner.PREV_CACHE.exists())
            finally:
                m12_runner.PREV_CACHE = old_cache
                SP.daily_prev_context = orig


class TestSelfHeal(unittest.TestCase):
    """The recovery path that was missing for M12/M13 (and now M14)."""

    def setUp(self):
        self._cache_files = SP.CACHE_FILES
        self._daily = SP.daily_prev_context
        self._td = tempfile.TemporaryDirectory()
        SP.CACHE_FILES = {
            "m12": Path(self._td.name) / "m12_prev_close.json",
            "m13": Path(self._td.name) / "m13_prev_context.json",
            "m14": Path(self._td.name) / "m14_prev_context.json",
        }

    def tearDown(self):
        SP.CACHE_FILES = self._cache_files
        SP.daily_prev_context = self._daily
        self._td.cleanup()

    @staticmethod
    def _fake_daily(n_ok, day="2026-08-28"):
        def _fake(symbols, day_, **kw):
            symbols = list(symbols)
            got = symbols[:n_ok]
            return {"date": str(day_ or day), "close": {s: 99.5 for s in got},
                    "pivot": {s: 100.0 for s in got}, "count": len(got),
                    "tried": len(symbols), "last_bar_min": "15:25"}
        return _fake

    def test_self_heal_targets_previous_session(self):
        """A Monday heal must seed Friday's closes, not Monday's live bars."""
        seen = {}

        def _fake(symbols, day, **kw):
            seen["day"] = str(day)
            seen["rng"] = kw.get("rng")
            symbols = list(symbols)
            return {"date": str(day), "close": {s: 99.5 for s in symbols},
                    "pivot": {s: 100.0 for s in symbols}, "count": len(symbols),
                    "tried": len(symbols), "last_bar_min": "15:25"}

        SP.daily_prev_context = _fake
        ok, meta = SP.self_heal("m12", "2026-08-31", deadline_s=30.0)
        self.assertTrue(ok)
        self.assertEqual(seen["day"], "2026-08-28")     # Fri, not Mon
        self.assertEqual(seen["rng"], "1mo")            # survives the weekend
        self.assertEqual(meta["status"], "OK")

    def test_self_heal_refuses_partial_and_leaves_cache_absent(self):
        SP.daily_prev_context = self._fake_daily(3)     # the 08-28 poison size
        ok, meta = SP.self_heal("m12", "2026-08-31", deadline_s=30.0)
        self.assertFalse(ok)
        self.assertEqual(meta["status"], "INSUFFICIENT")
        self.assertFalse(SP.CACHE_FILES["m12"].exists())

    def test_self_heal_writes_only_when_complete(self):
        SP.daily_prev_context = self._fake_daily(210)
        ok, meta = SP.self_heal("m12", "2026-08-31", deadline_s=30.0)
        self.assertTrue(ok)
        j = json.loads(SP.CACHE_FILES["m12"].read_text())
        self.assertEqual(j["date"], "2026-08-28")
        self.assertEqual(j["count"], 210)
        self.assertEqual(j["status"], "OK")

    def test_self_heal_seeds_all_models_from_one_network_pass(self):
        """m12+m13+m14 together must cost one pass, not three."""
        calls = []

        def _fake(symbols, day, **kw):
            calls.append(1)
            symbols = list(symbols)
            return {"date": str(day), "close": {s: 99.5 for s in symbols},
                    "pivot": {s: 100.0 for s in symbols}, "count": len(symbols),
                    "tried": len(symbols), "last_bar_min": "15:25"}

        SP.daily_prev_context = _fake
        SP.seed_models(["m12", "m13", "m14"], "2026-08-28", deadline_s=30.0)
        self.assertEqual(len(calls), 1)

    def test_should_self_heal_is_once_per_hour(self):
        st: dict = {}
        self.assertTrue(SP.should_self_heal(st, "2026-08-31", "09:20"))
        SP.mark_self_heal(st, "2026-08-31", "09:20")
        self.assertFalse(SP.should_self_heal(st, "2026-08-31", "09:45"))
        self.assertTrue(SP.should_self_heal(st, "2026-08-31", "10:05"))

    def test_daily_prev_context_retries_transient_misses(self):
        """A single 429 must not silently drop a symbol from the seed."""
        attempts = []

        def _flaky(sym, rng):
            attempts.append(sym)
            return None if len(attempts) == 1 else bars(
                "2026-08-28", [("15:25", 100.0, 99.0, 99.5)])

        ctx = SP.daily_prev_context(["AAA"], "2026-08-28", fetch=_flaky,
                                    sleep_s=0.0, deadline_s=10, rng="1mo")
        self.assertEqual(len(attempts), 2)          # retried once
        self.assertEqual(ctx["count"], 1)
        self.assertEqual(ctx["tried"], 1)           # still one symbol, not two


class TestDates(unittest.TestCase):
    def test_expected_previous_weekday(self):
        self.assertEqual(SP.expected_previous_weekday(pd.Timestamp("2026-08-31").date()),
                         pd.Timestamp("2026-08-28").date())  # Mon -> Fri
        self.assertEqual(SP.expected_previous_weekday(pd.Timestamp("2026-09-01").date()),
                         pd.Timestamp("2026-08-31").date())  # Tue -> Mon

    def test_latest_closed_session(self):
        import datetime as dt
        mon_am = dt.datetime(2026, 8, 31, 10, 0, tzinfo=SP.IST)
        mon_pm = dt.datetime(2026, 8, 31, 16, 10, tzinfo=SP.IST)
        self.assertEqual(str(SP.latest_closed_session(mon_am)), "2026-08-28")
        self.assertEqual(str(SP.latest_closed_session(mon_pm)), "2026-08-31")


if __name__ == "__main__":
    unittest.main()
