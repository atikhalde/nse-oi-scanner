"""Unit tests for M14 Ultra High-Conviction A+ Entry Engine."""
import unittest
import numpy as np
import pandas as pd
import m14_entry as E


class TestM14Entry(unittest.TestCase):

    def setUp(self):
        dates = pd.date_range("2026-08-28 09:15", "2026-08-28 10:00", freq="5min", tz="Asia/Kolkata")
        n = len(dates)
        np.random.seed(42)
        base = 100.0 + np.cumsum(np.random.randn(n) * 0.2)

        df = pd.DataFrame({
            "dt": dates,
            "open": base,
            "high": base + 0.3,
            "low": base - 0.3,
            "close": base + 0.1,
            "volume": [10000] * n,
        }).set_index("dt")

        self.engine_frame = df
        self.today_prefix = df.reset_index()

    def test_allowed_codes(self):
        self.assertIn(102, E.ALLOWED_CODES)
        self.assertIn(209, E.ALLOWED_CODES)
        self.assertIn(280, E.ALLOWED_CODES)
        self.assertNotIn(90, E.ALLOWED_CODES)
        self.assertNotIn(290, E.ALLOWED_CODES)

    def test_causal_features(self):
        feats = E.causal_features(
            engine_frame=self.engine_frame,
            today_prefix=self.today_prefix,
            side="BUY",
            prev_close=100.0,
            spurt_rank=5,
            master_total_score=80.0,
            opening_breadth_dir=0.60,
            live_breadth_dir=0.55,
            sector_breadth_prev_dir=0.30,
            prior_vix_return=1.0,
            video_setups=["S1", "S3"],
            day_regime="MIXED",
        )
        self.assertEqual(feats["spurt_rank"], 5)
        self.assertEqual(feats["video_setup_count"], 2)
        self.assertGreaterEqual(feats["candle_clv"], 0.0)
        self.assertLessEqual(feats["candle_clv"], 1.0)

    def test_decide_accepted(self):
        feats = {
            "dir_prev_pct": 0.5,
            "dir_gap_pct": 0.2,
            "ema9_20_atr": 0.5,
            "ema20_50_atr": 0.8,
            "close_ema20_atr": 0.5,
            "close_vwap_atr": 0.4,
            "candle_clv": 0.80,
            "body_atr": 0.5,
            "range_atr": 1.0,
            "relvol20": 1.5,
            "clock_relvol": 1.5,
            "spurt_rank": 3,
            "master_total_score": 85.0,
            "opening_breadth_dir": 0.60,
            "live_breadth_dir": 0.55,
            "sector_breadth_prev_dir": 0.25,
            "prior_vix_return": 1.0,
            "video_setups": "S1+S3",
            "video_setup_count": 2,
            "s1": 1,
            "s2": 0,
            "s3": 1,
            "s4": 0,
            "day_regime": "MIXED",
        }
        dec = E.decide(102, "BUY-EX17", "BUY", "10:00", feats)
        self.assertTrue(dec.accepted)
        self.assertEqual(dec.reason, "qualified")
        self.assertGreaterEqual(dec.score, 75.0)

    def test_decide_rejected_stale_window(self):
        feats = {"spurt_rank": 3, "video_setup_count": 1, "clock_relvol": 1.2}
        dec = E.decide(102, "BUY-EX17", "BUY", "12:30", feats)
        self.assertFalse(dec.accepted)
        self.assertIn("peak morning window", dec.reason)

    def test_decide_rejected_preview_code(self):
        feats = {"spurt_rank": 3}
        dec = E.decide(90, "ENTRY BUY", "BUY", "10:00", feats)
        self.assertFalse(dec.accepted)
        self.assertIn("blocked", dec.reason)

    def test_decide_rejected_high_spurt_rank(self):
        feats = {
            "spurt_rank": 15,
            "video_setup_count": 1,
            "clock_relvol": 1.5,
            "candle_clv": 0.8,
            "opening_breadth_dir": 0.60,
            "dir_prev_pct": 0.5,
            "sector_breadth_prev_dir": 0.2,
            "prior_vix_return": 1.0,
            "day_regime": "MIXED",
        }
        dec = E.decide(102, "BUY-EX17", "BUY", "10:00", feats)
        self.assertFalse(dec.accepted)
        self.assertIn("spurt rank", dec.reason)


if __name__ == "__main__":
    unittest.main()
