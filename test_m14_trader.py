"""Unit tests for M14 Ultra High-Conviction Paper Trade Manager."""
import unittest
import pandas as pd
import m14_trader as T


class TestM14Trader(unittest.TestCase):

    def setUp(self):
        dates = pd.date_range("2026-08-28 09:15", "2026-08-28 15:20", freq="5min", tz="Asia/Kolkata")
        n = len(dates)
        # Create a sample price series where stock moves up from 100 to 105 then consolidates
        prices = [100.0 + i * 0.1 for i in range(n)]

        self.bars = pd.DataFrame({
            "dt": dates,
            "t": dates.strftime("%H:%M"),
            "open": prices,
            "high": [p + 0.2 for p in prices],
            "low": [p - 0.2 for p in prices],
            "close": [p + 0.1 for p in prices],
            "volume": [5000] * n,
        })

    def test_evaluate_buy_trade(self):
        tr = T.evaluate("RELIANCE", "BUY", "09:45", 100.0, "BUY-EX17", self.bars)
        self.assertNotIn("error", tr)
        self.assertEqual(tr["symbol"], "RELIANCE")
        self.assertEqual(tr["side"], "BUY")
        self.assertEqual(tr["entry"], 100.0)
        self.assertGreater(tr["qty"], 0)
        self.assertLessEqual(tr["capital"], 50000)
        self.assertLessEqual(tr["risk_rs"], 900)
        self.assertTrue(tr["closed"])

    def test_evaluate_notional_and_risk_cap(self):
        # Expensive stock (e.g. 50,000 entry) -> max 1 share notional
        expensive_bars = self.bars.copy()
        for col in ["open", "high", "low", "close"]:
            expensive_bars[col] = expensive_bars[col] * 500.0

        tr = T.evaluate("MRF", "BUY", "09:45", 50000.0, "BUY-EX17", expensive_bars)
        self.assertNotIn("error", tr)
        self.assertEqual(tr["qty"], 1)
        self.assertLessEqual(tr["capital"], 50000)

    def test_fmt_alert(self):
        tr = T.evaluate("RELIANCE", "BUY", "09:45", 100.0, "BUY-EX17", self.bars)
        entry_text = T.fmt_alert(tr, "ENTRY")
        self.assertIn("ENTRY", entry_text)
        self.assertIn("RELIANCE", entry_text)

        exit_text = T.fmt_alert(tr, "EXIT_EOD")
        self.assertIn("RELIANCE", exit_text)


if __name__ == "__main__":
    unittest.main()
