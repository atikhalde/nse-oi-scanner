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
    def setUp(self):
        patcher = patch.object(F, "_COOLDOWN_UNTIL", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_yahoo_only_even_if_dhan_configured(self):
        with patch.object(F.feeds, 'dhan_ok', return_value=True), patch.object(
                F.feeds, 'fetch_bars_dhan') as dhan, patch.object(
                F.feeds, 'fetch_bars_yahoo', return_value=frame()) as yahoo:
            bars, health = F.fetch('TEST', '1', NOW)
        self.assertEqual(health['provider'], 'yahoo')
        self.assertEqual(yahoo.call_args.args, ('TEST', '1d'))
        dhan.assert_not_called()

    def test_no_token_or_security_id_required(self):
        with patch.dict('os.environ', {}, clear=True), patch.object(
                F.feeds, 'fetch_bars_yahoo', return_value=frame()):
            _, health = F.fetch('TEST', None, NOW)
        self.assertEqual(health['provider'], 'yahoo')

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

    def test_empty_yahoo_response_reports_failure(self):
        with patch.object(F.feeds, 'fetch_bars_yahoo', return_value=None):
            with self.assertRaisesRegex(ValueError, 'yahoo.*empty response'):
                F.fetch('TEST', None, NOW)

    def test_empty_one_day_window_recovers_via_five_day_window(self):
        with patch.object(F.feeds, 'fetch_bars_yahoo', side_effect=[None, frame()]) as fetch:
            _, health = F.fetch('TEST', None, NOW)
        self.assertEqual([x.args[1] for x in fetch.call_args_list], ['1d', '5d'])
        self.assertEqual(health['window'], '5d')
        self.assertIn('empty', health['fallback_errors'][0]['reason'])

    def test_rate_limit_is_reported_without_extra_range_hammering(self):
        def limited(*args, **kwargs):
            kwargs['on_error'](dict(http_status=429, host='query1', reason='HTTP error'))
            return None
        with patch.object(F.feeds, 'fetch_bars_yahoo', side_effect=limited) as fetch:
            with self.assertRaisesRegex(ValueError, '429'):
                F.fetch('TEST', None, NOW)
        self.assertEqual(fetch.call_count, 1)
        with patch.object(F.feeds, 'fetch_bars_yahoo') as blocked:
            with self.assertRaisesRegex(ValueError, '429 shared cooldown'):
                F.fetch('OTHER', None, NOW)
        blocked.assert_not_called()

    def test_unaligned_quote_does_not_advertise_candle_coverage(self):
        raw = pd.concat([frame(), frame('10:00:13')], ignore_index=True)
        clean = F.current_bars(raw, NOW)
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean.iloc[-1]['dt'].strftime('%H:%M'), '09:55')

    def test_recovery_retries_continue_after_first_thirty_seconds(self):
        target = NOW.floor('5min')-pd.Timedelta(minutes=5)
        self.assertEqual(R.discovery_retries(['TEST'], {}, NOW), {'TEST'})
        self.assertEqual(R.discovery_retries(['TEST'], {'TEST': (frame(), NOW)}, NOW), set())
        self.assertEqual(R.discovery_retries(['TEST'], {}, NOW.replace(hour=11)), set())

    def test_receipt_fresh_running_snapshot_survives_ten_second_request(self):
        bars = frame('10:00')
        bars.attrs['m16_feed'] = dict(received_at=NOW.isoformat())
        self.assertTrue(R.running_snapshot_usable(bars, NOW-pd.Timedelta(seconds=10), NOW))
        self.assertFalse(R.running_snapshot_usable(bars, NOW-pd.Timedelta(seconds=31), NOW))
        self.assertFalse(R.running_snapshot_usable(bars, NOW, NOW+pd.Timedelta(seconds=6)))

    def test_unseeded_discovery_failure_is_not_called_healthy_after_eleven(self):
        st = M.fresh_state('2026-11-23')
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, NOW.replace(hour=11), True)
        self.assertEqual(st['health_notice']['status'], 'degraded')
        self.assertIn('TEST', st['monitoring_health']['details'])

    def test_shared_yahoo_reports_status_without_changing_default_callers(self):
        import requests
        from unittest.mock import MagicMock
        response = MagicMock(status_code=429)
        response.raise_for_status.side_effect = requests.HTTPError('redacted')
        session = MagicMock()
        session.get.return_value = response
        errors = []
        with patch.object(F.feeds, '_yahoo_session', return_value=(session, None)):
            result = F.feeds.fetch_bars_yahoo('TEST', on_error=errors.append, timeout=(2, 3))
        self.assertIsNone(result)
        self.assertEqual(sum(x['http_status'] == 429 for x in errors), 2)
        self.assertEqual(session.get.call_args.kwargs['timeout'], (2, 3))

    def test_outage_episode_survives_success_and_records_recovery(self):
        st = M.fresh_state('2026-11-23')
        R.record_feed_failure(st, 'TEST', NOW, 'empty')
        R.record_feed_failure(st, 'TEST', NOW+pd.Timedelta(seconds=5), 'empty')
        R.record_feed_success(st, 'TEST', NOW+pd.Timedelta(seconds=10), dict(provider='yahoo'))
        episode = st['feed_outages']['TEST'][0]
        self.assertEqual(episode['count'], 2)
        self.assertEqual(episode['start'], NOW.isoformat())
        self.assertIn('recovered_at', episode)
        R.record_feed_failure(st, 'TEST', NOW+pd.Timedelta(seconds=20), 'empty')
        self.assertEqual(len(st['feed_outages']['TEST']), 2)

    def test_poisoned_session_recovery_is_throttled(self):
        with patch.object(F, '_LAST_SESSION_RESET', float('-inf')), patch.object(
                F.feeds, '_YH_SESSION', ('old-session', 'old-crumb')):
            self.assertTrue(F.recover_yahoo_session(NOW))
            self.assertIsNone(F.feeds._YH_SESSION)
            self.assertFalse(F.recover_yahoo_session(NOW))

    def test_duplicate_failures_preserve_first_and_count(self):
        st = M.fresh_state('2026-11-23')
        for i in range(100):
            R.record_feed_failure(st, 'TEST', NOW+pd.Timedelta(seconds=i), 'empty')
        self.assertEqual(len(st['diagnostics']), 2)
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

    def test_normal_five_minute_discovery_cadence_is_not_an_outage(self):
        st = M.fresh_state('2026-11-23')
        st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat(), latest=frame().iloc[-1]['dt'].isoformat())}
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, NOW+pd.Timedelta(minutes=3), True)
            self.assertEqual(st['health_notice']['status'], 'healthy')
            R.notify_health(st, NOW+pd.Timedelta(minutes=5), True)
            self.assertEqual(st['health_notice']['status'], 'degraded')

    def test_fresh_feed_cannot_hide_stalled_analysis_or_missing_history(self):
        st = M.fresh_state('2026-11-23')
        st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat(), latest=frame().iloc[-1]['dt'].isoformat())}
        st['history_health'] = {'TEST': True}
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, NOW, True)
            self.assertEqual(st['health_notice']['status'], 'degraded')
            st['cursors']['TEST'] = frame().iloc[-1]['dt'].isoformat()
            R.notify_health(st, NOW, True)
            self.assertEqual(st['health_notice']['status'], 'healthy')
            st['history_health']['TEST'] = False
            R.notify_health(st, NOW, True)
            self.assertEqual(st['health_notice']['status'], 'degraded')

    def test_health_notices_once_per_session_even_after_flapping_and_restart(self):
        import json
        st = M.fresh_state('2026-11-23')
        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R, 'deliver_targets', return_value=True) as send:
            R.notify_health(st, NOW)
            R.notify_health(st, NOW+pd.Timedelta(minutes=6))
            R.notify_health(st, NOW+pd.Timedelta(minutes=12))
            self.assertEqual(send.call_count, 1)
            st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat())}
            R.notify_health(st, NOW)
            self.assertEqual(send.call_count, 2)
            st = json.loads(json.dumps(st))
            st['feed_health'] = {}
            R.notify_health(st, NOW+pd.Timedelta(minutes=20))
            st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat())}
            R.notify_health(st, NOW)
            self.assertEqual(send.call_count, 2)
        self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_preexisting_delivered_notice_is_not_repeated_after_upgrade(self):
        st = M.fresh_state('2026-11-23')
        st['health_notice'] = dict(status='degraded', at=NOW.isoformat(), complete=True, deliveries={})
        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(R, 'deliver_targets') as send:
            R.notify_health(st, NOW+pd.Timedelta(minutes=10))
        send.assert_not_called()

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
