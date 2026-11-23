"""No-network regressions for incident recovery and M16-only data routing."""
import unittest
from unittest.mock import patch
import pandas as pd
import m16_feed as F
import m16_runner as R
import m16_model as M

NOW = pd.Timestamp('2026-11-23 10:00:31', tz='Asia/Kolkata')


def frame(t='09:55', day='2026-11-23'):
    return pd.DataFrame([dict(dt=pd.Timestamp(f'{day} {t}', tz='Asia/Kolkata'),
                              open=100, high=101, low=99, close=100, volume=10)])


class FeedTests(unittest.TestCase):
    def test_dhan_primary_does_not_call_yahoo(self):
        with patch.object(F.feeds, 'dhan_ok', return_value=True), patch.object(
                F.feeds, 'fetch_bars_dhan', return_value=frame()), patch.object(
                F.feeds, 'fetch_bars_yahoo') as yahoo:
            bars, health = F.fetch('TEST', '1', NOW)
        self.assertEqual(health['provider'], 'dhan')
        yahoo.assert_not_called()

    def test_stale_nonempty_primary_falls_back(self):
        with patch.object(F.feeds, 'dhan_ok', return_value=True), patch.object(
                F.feeds, 'fetch_bars_dhan', return_value=frame('09:30')), patch.object(
                F.feeds, 'fetch_bars_yahoo', return_value=frame()):
            bars, health = F.fetch('TEST', '1', NOW)
        self.assertEqual(health['provider'], 'yahoo')
        self.assertIn('stale', health['fallback_errors'][0]['reason'])

    def test_previous_session_nonempty_is_not_usable(self):
        with self.assertRaisesRegex(ValueError, 'current-session'):
            F.current_bars(frame(day='2026-11-20'), NOW)

    def test_publication_grace_then_reject(self):
        F.current_bars(frame('09:50'), NOW-pd.Timedelta(seconds=2))
        with self.assertRaisesRegex(ValueError, 'stale'):
            F.current_bars(frame('09:50'), NOW)

    def test_all_stale_providers_fail_with_context(self):
        with patch.object(F.feeds, 'dhan_ok', return_value=False), patch.object(
                F.feeds, 'fetch_bars_yahoo', return_value=frame('09:30')):
            with self.assertRaisesRegex(ValueError, 'yahoo.*stale'):
                F.fetch('TEST', None, NOW)

    def test_forming_candle_does_not_hide_missing_completed_candle(self):
        with self.assertRaisesRegex(ValueError, 'missing completed'):
            F.current_bars(frame('10:00'), NOW)

    def test_rate_limit_cools_down_provider_and_uses_fallback(self):
        import requests
        exc = requests.HTTPError()
        exc.response = requests.Response()
        exc.response.status_code = 429
        with patch.dict(F._COOLDOWN, {}, clear=True), patch.object(
                F.feeds, 'dhan_ok', return_value=True), patch.object(
                F.feeds, 'fetch_bars_dhan', side_effect=exc) as dhan, patch.object(
                F.feeds, 'fetch_bars_yahoo', return_value=frame()):
            _, first = F.fetch('TEST', '1', NOW)
            _, second = F.fetch('TEST', '1', NOW)
        self.assertEqual(first['provider'], 'yahoo')
        self.assertEqual(first['fallback_errors'][0]['http_status'], 429)
        self.assertEqual(dhan.call_count, 1)
        self.assertIn('cooldown', second['fallback_errors'][0]['reason'])

    def test_duplicate_failures_preserve_first_and_count(self):
        st = M.fresh_state('2026-11-23')
        for i in range(100):
            R.record_feed_failure(st, 'TEST', NOW+pd.Timedelta(seconds=i), 'empty')
        self.assertEqual(len(st['diagnostics']), 1)
        self.assertEqual(st['feed_health']['TEST']['failure_count'], 100)
        self.assertEqual(st['feed_health']['TEST']['first_failure'], NOW.isoformat())

    def test_health_degradation_recovery_and_no_spam(self):
        st = M.fresh_state('2026-11-23')
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, NOW, True)
            self.assertEqual(st['health_notice']['status'], 'degraded')
            initial = st['health_notice']['at']
            R.notify_health(st, NOW+pd.Timedelta(seconds=30), True)
            self.assertEqual(st['health_notice']['at'], initial)
            st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat())}
            R.notify_health(st, NOW+pd.Timedelta(seconds=31), True)
            self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_failed_health_delivery_is_retried(self):
        st = M.fresh_state('2026-11-23')
        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R, 'deliver_targets', side_effect=[False, True]) as send:
            R.notify_health(st, NOW)
            R.notify_health(st, NOW+pd.Timedelta(seconds=30))
        self.assertEqual(send.call_count, 2)
        self.assertTrue(st['health_notice']['complete'])


if __name__ == '__main__':
    unittest.main()
