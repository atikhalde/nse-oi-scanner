"""Unit tests for M14 Ultra High-Conviction Paper Runner."""
import json
import tempfile
import unittest
from pathlib import Path
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

    def test_load_prev_fail_closed_on_stale_history(self):
        # When cache or history does not match expected_prev, return STALE
        orig_cache = R.PREV_CACHE
        R.PREV_CACHE = self.tmp_path / "non_existent_cache.json"
        try:
            vals, pivs, meta = R.load_prev("2026-08-28")
            # If historical data files are stale (e.g. 2026-08-18 vs 2026-08-27 expected), status must be STALE
            if meta.get("date") != "2026-08-27":
                self.assertEqual(meta["status"], "STALE")
        finally:
            R.PREV_CACHE = orig_cache

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


if __name__ == "__main__":
    unittest.main()
