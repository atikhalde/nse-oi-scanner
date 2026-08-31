"""Unit tests for M14 Ultra High-Conviction Paper Runner."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pandas as pd
import m14_runner as R


class TestM14Runner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def test_state_load_save(self):
        state_file = self.tmp_path / "state14.json"
        st = {"date": "2026-08-28", "trades": {}, "alerts": [], "decisions": []}
        state_file.write_text(json.dumps(st))

        # Backup R.STATE
        orig_state = R.STATE
        R.STATE = state_file
        try:
            loaded = R.load_state("2026-08-28")
            self.assertEqual(loaded["date"], "2026-08-28")

            # Load different date resets state
            new_day = R.load_state("2026-08-29")
            self.assertEqual(new_day["date"], "2026-08-29")
            self.assertEqual(len(new_day["trades"]), 0)
        finally:
            R.STATE = orig_state

    def test_expected_prev_weekday(self):
        # Monday 2026-08-31 -> expected prev is Friday 2026-08-28
        mon = R.parse_day("2026-08-31")
        exp = R.expected_prev(mon)
        self.assertEqual(exp, R.parse_day("2026-08-28"))

        # Tuesday 2026-09-01 -> expected prev is Monday 2026-08-31
        tue = R.parse_day("2026-09-01")
        exp_tue = R.expected_prev(tue)
        self.assertEqual(exp_tue, R.parse_day("2026-08-31"))

    def test_load_prev_degraded_fallback_on_stale_history(self):
        # When cache/history lag the expected prior weekday but history exists,
        # return a usable FALLBACK baseline (M11-style) so M14 still scans and
        # fires, instead of failing closed for the whole session. The date is
        # recorded as the actual (degraded) baseline for the audit trail.
        orig_cache = R.PREV_CACHE
        R.PREV_CACHE = self.tmp_path / "non_existent_cache.json"
        try:
            vals, pivs, meta = R.load_prev("2026-08-28")
            # If historical data files are stale (e.g. 2026-08-18 vs 2026-08-27
            # expected), status must be FALLBACK (usable) and still carry data.
            if meta.get("date") != "2026-08-27":
                self.assertEqual(meta["status"], "FALLBACK")
                self.assertGreaterEqual(meta.get("count", 0), 1)
                self.assertEqual(len(vals), meta.get("count"))
        finally:
            R.PREV_CACHE = orig_cache

    def test_load_prev_stale_on_no_history(self):
        # If there is genuinely no history/cache at all, still fail closed STALE.
        orig_cache = R.PREV_CACHE
        orig_hist = R.L.HIST
        try:
            R.PREV_CACHE = self.tmp_path / "non_existent_cache.json"
            R.L.HIST = self.tmp_path / "empty_history"   # directory with no CSVs
            vals, pivs, meta = R.load_prev("2026-08-28")
            self.assertEqual(meta["status"], "STALE")
            self.assertEqual(vals, {})
        finally:
            R.PREV_CACHE = orig_cache
            R.L.HIST = orig_hist

    def test_portfolio_limits(self):
        st = {
            "date": "2026-08-28",
            "trades": {
                "RELIANCE": {"symbol": "RELIANCE", "side": "BUY", "closed": False, "m14_sector": "OIL"},
                "INFY": {"symbol": "INFY", "side": "BUY", "closed": False, "m14_sector": "IT"},
            },
            "alerts": [],
            "decisions": [],
        }

        self.assertEqual(R.trade_count(st), 2)
        self.assertEqual(R.open_count(st), 2)
        self.assertIn("RELIANCE", R.taken_symbols(st))
        self.assertIn("OIL", R.taken_sectors(st))

    def _bars(self, last_t: str):
        t = pd.date_range("2026-08-28 09:15", periods=3, freq="5min", tz="Asia/Kolkata")
        d = pd.DataFrame({"dt": t, "open": [100.0] * 3, "high": [101.0] * 3,
                          "low": [99.0] * 3, "close": [100.5] * 3, "volume": [1000] * 3})
        d["t"] = d["dt"].dt.strftime("%H:%M")
        d.loc[d.index[-1], "t"] = last_t
        return d

    def test_seed_prev_coverage_guard(self):
        """Partial (<180) EOD seeds must never overwrite the prev-close cache.

        The 2026-08-28 M12/M13 cache was overwritten with 3 symbols because the
        15:25 cycle's Yahoo feed had not yet delivered 15:20+ bars for most names;
        M12/M13/M14 then stayed STALE for 2026-08-31.
        The daily top-up is disabled here so this test stays offline: the
        top-up path itself is covered in test_seed_prev_context.py.
        """
        old_cache = R.PREV_CACHE
        env = {"SEED_PREV_DAILY_TOPUP": "0"}
        try:
            with tempfile.TemporaryDirectory() as td:
                R.PREV_CACHE = Path(td) / "m14_prev_context.json"
                full = {f"S{i}": self._bars("15:20") for i in range(180)}
                full["LATE"] = self._bars("15:15")
                with mock.patch.dict(os.environ, env):
                    j = R.seed_prev("2026-08-28", full)
                self.assertEqual(j["status"], "OK")
                self.assertTrue(R.PREV_CACHE.exists())
                saved = json.loads(R.PREV_CACHE.read_text())
                self.assertNotIn("LATE", saved.get("close") or {})
                self.assertEqual(len(saved["close"]), 180)

                R.PREV_CACHE.write_text(json.dumps({"date": "2026-08-27", "close": {"KEEP": 1.0}}))
                with mock.patch.dict(os.environ, env):
                    j = R.seed_prev("2026-08-28", {f"P{i}": self._bars("15:20") for i in range(3)})
                self.assertEqual(j["status"], "INSUFFICIENT")
                saved = json.loads(R.PREV_CACHE.read_text())
                self.assertIn("KEEP", saved.get("close") or {})
        finally:
            R.PREV_CACHE = old_cache


if __name__ == "__main__":
    unittest.main()
