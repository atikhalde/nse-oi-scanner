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

    def test_previous_session_response_does_not_double_range_requests(self):
        with patch.object(F.feeds,'fetch_bars_yahoo',return_value=frame(day='2026-11-20')) as fetch:
            with self.assertRaisesRegex(ValueError,'source_latest'):
                F.fetch('TEST',None,NOW)
        self.assertEqual(fetch.call_count,1)

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
        self.assertEqual(sum(x['http_status'] == 429 for x in errors), 1)
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

    def test_shared_typed_api_preserves_retry_after_and_callback(self):
        from unittest.mock import MagicMock
        response = MagicMock(status_code=429, headers={'Retry-After': '90'})
        session = MagicMock()
        session.get.return_value = response
        errors = []
        with patch.object(F.feeds, '_yahoo_session', return_value=(session, None)):
            with self.assertRaises(F.feeds.YahooFetchError) as caught:
                F.feeds.fetch_bars_yahoo('TEST', raise_errors=True, on_error=errors.append)
        self.assertEqual(caught.exception.retry_after, 90)
        self.assertEqual(errors[0]['http_status'], 429)
        self.assertEqual(session.get.call_count, 1)

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

    def test_sector_block_notice_names_state_last_board_and_reason(self):
        st = M.fresh_state('2026-11-23')
        st['sector_gate_required'] = True
        st['feed_health'] = {'TEST': dict(ok=True, at=NOW.isoformat(),
                                          latest=frame().iloc[-1]['dt'].isoformat())}
        st['sector_snapshots'] = [dict(close='2026-11-23T09:20:00+05:30',
                                       observed='2026-11-23T09:20:20+05:30',
                                       sectors={}, complete=True)]
        st['sector_health'] = dict(ok=False, active=True, at=NOW.isoformat(),
                                   reason='sector board global/index inputs missing for target: 09:20')
        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R.m16_sector, 'decision', return_value={'status': 'unknown',
                                                        'reason': 'sector board stale'}), patch(
                'builtins.print') as out:
            R.notify_health(st, NOW, True)
        text = out.call_args.args[0]
        self.assertIn('sector board missing/stale', text)
        self.assertIn('state: stale', text)
        self.assertIn('last board 09:20', text)
        self.assertIn('refresh failing', text)
        self.assertEqual(st['health_notice']['status'], 'degraded')

    def test_sector_recovery_is_announced_once_then_bounded(self):
        st = M.fresh_state('2026-11-23')
        st['sector_gate_required'] = True
        st['sector_snapshots'] = [dict(close='2026-11-23T09:20:00+05:30',
                                       observed='2026-11-23T09:20:20+05:30',
                                       sectors={}, complete=True)]
        state = {'blocked': True}

        def decide(*_a, **_k):
            return ({'status': 'unknown', 'reason': 'sector board stale'} if state['blocked']
                    else {'status': 'unknown', 'reason': 'home sector unavailable'})

        texts = []

        def send(_st, ledger, sender, can_send=lambda: True):
            for token, chat in R.telegram_targets():
                key = R.target_id(token, chat)
                if ledger.get(key) in {'sent', 'sending'}:
                    continue
                ledger[key] = 'sending'
                self.assertTrue(sender(token, chat))
            return True

        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R.m16_sector, 'decision', side_effect=decide), patch.object(
                R, 'deliver_targets', side_effect=send), patch.object(
                R, 'telegram_targets', return_value=[('tok', 'chat')]), patch.object(
                R, 'save'), patch.object(R.tg, 'send_message_to',
                                         side_effect=lambda _t, _c, text: texts.append(text) or True):
            def tick(minutes, blocked):
                state['blocked'] = blocked
                now = NOW + pd.Timedelta(minutes=minutes)
                # A live run refreshes feed health each cycle; keep it current so
                # these assertions exercise the sector notice, not feed staleness.
                st['feed_health'] = {'TEST': dict(ok=True, at=now.isoformat(),
                                                  latest=R.m16_feed.required_closed_bar(now).isoformat())}
                R.notify_health(st, now)

            tick(0, True)     # degraded block, sent
            tick(0.5, True)   # deduped
            tick(1, False)    # first healthy, sent
            tick(6, True)     # deduped block
            tick(9, False)    # recovery #1
            tick(15, True)    # deduped
            tick(18, False)   # recovery #2
            tick(24, True)    # deduped
            tick(27, False)   # capped: silent
        self.assertEqual(len(texts), 4, texts)
        self.assertEqual(sum('M16 RECOVERED' in t for t in texts), 2)
        self.assertEqual(st['health_recoveries'], 2)
        self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_failed_recovery_keeps_degraded_marker_and_retries(self):
        st = M.fresh_state('2026-11-23')
        st['sector_gate_required'] = True
        st['sector_snapshots'] = [dict(close='2026-11-23T09:20:00+05:30',
                                       observed='2026-11-23T09:20:20+05:30',
                                       sectors={}, complete=True)]
        st['health_notices'] = {
            'healthy': dict(status='healthy', complete=True, at=NOW.isoformat(), deliveries={}),
            'degraded': dict(status='degraded', complete=True,
                             at=NOW.isoformat(), deliveries={})}
        st['health_notice'] = dict(st['health_notices']['degraded'])
        outcomes = [False, True]
        texts = []

        def send(_st, ledger, sender, can_send=lambda: True):
            for token, chat in R.telegram_targets():
                key = R.target_id(token, chat)
                if ledger.get(key) in {'sent', 'sending'}:
                    continue
                ledger[key] = 'sent' if sender(token, chat) else 'failed'
            return all(v == 'sent' for v in ledger.values())

        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R.m16_sector, 'decision', return_value={'status': 'unknown',
                                                        'reason': 'home sector unavailable'}), patch.object(
                R, 'deliver_targets', side_effect=send), patch.object(
                R, 'telegram_targets', return_value=[('tok', 'chat')]), patch.object(
                R, 'save'), patch.object(R.tg, 'send_message_to',
                                         side_effect=lambda _t, _c, text: texts.append(text) or outcomes.pop(0)):
            for minute in (3, 3.5, 4):
                now = NOW + pd.Timedelta(minutes=minute)
                st['feed_health'] = {'TEST': dict(ok=True, at=now.isoformat(),
                                                  latest=R.m16_feed.required_closed_bar(now).isoformat())}
                R.notify_health(st, now)
        self.assertEqual(len(texts), 2, texts)
        self.assertTrue(all('M16 RECOVERED' in t for t in texts))
        self.assertEqual(st['health_notice']['status'], 'healthy')
        self.assertEqual(st['health_recoveries'], 1)

    def test_failed_health_delivery_is_retried(self):
        st = M.fresh_state('2026-11-23')
        with patch.object(R.shared.L, 'SYMS', ['TEST']), patch.object(
                R, 'deliver_targets', side_effect=[False, True]) as send:
            R.notify_health(st, NOW)
            R.notify_health(st, NOW+pd.Timedelta(seconds=30))
        self.assertEqual(send.call_count, 2)
        self.assertTrue(st['health_notice']['complete'])

    def test_empty_symbol_does_not_invalidate_healthy_shared_session(self):
        session = ('working-session', 'working-crumb')
        with patch.object(F.feeds, '_YH_SESSION', session), patch.object(
                F.feeds, 'fetch_bars_yahoo', side_effect=[None, None, frame()]):
            with self.assertRaisesRegex(ValueError, 'empty response'):
                F.fetch('MISSING', None, NOW)
            self.assertIs(F.feeds._YH_SESSION, session)
            _, health = F.fetch('HEALTHY', None, NOW)
            self.assertEqual(health['provider'], 'yahoo')
            self.assertIs(F.feeds._YH_SESSION, session)

    def test_fetch_admission_is_bounded_and_fast_lane_is_reserved(self):
        symbols = {f'S{i:03}' for i in range(210)}
        got = list(R.fetch_candidates(symbols, {'S209'}, set(), {}, set(), {}))
        self.assertEqual(got[0], 'S209')
        self.assertEqual(len(got), 17)
        self.assertEqual(list(R.fetch_candidates(symbols, {'S209'}, set(), {},
                         set(), {i: False for i in range(16)})), ['S209'])

    def test_fetch_admission_fairness_and_lane_changes(self):
        got = list(R.fetch_candidates({'A','B'}, set(), set(), {'A': 10}, set(),
                                     {i: False for i in range(15)}))
        self.assertEqual(got, ['B'])
        # A normal task becoming armed must not free its still occupied
        # normal executor slot, nor occupy a fast executor slot retroactively.
        got = list(R.fetch_candidates({'A','B'}, {'A'}, set(), {}, {'A'},
                                     {i: False for i in range(16)}))
        self.assertEqual(got, [])

    def test_closing_window_omitted_1515_and_1520_bars_do_not_fail_feed_or_health(self):
        # Yahoo omits 15:15 on ~35-80% of NSE equities and 15:20 on ~98% before
        # publishing 15:25 after 15:30 IST.
        only_1510 = frame('15:10')
        jump_to_1525 = pd.concat([frame('15:10'), frame('15:25')], ignore_index=True)
        t_1520 = pd.Timestamp('2026-11-23 15:20:35', tz='Asia/Kolkata')
        t_1526 = pd.Timestamp('2026-11-23 15:26:00', tz='Asia/Kolkata')
        t_1530_early = pd.Timestamp('2026-11-23 15:30:10', tz='Asia/Kolkata')
        t_1530_due = pd.Timestamp('2026-11-23 15:30:35', tz='Asia/Kolkata')
        t_1536 = pd.Timestamp('2026-11-23 15:36:00', tz='Asia/Kolkata')
        self.assertEqual(F.current_bars(only_1510, t_1520).iloc[-1]['dt'].strftime('%H:%M'), '15:10')
        self.assertEqual(F.current_bars(only_1510, t_1526).iloc[-1]['dt'].strftime('%H:%M'), '15:10')
        self.assertEqual(F.current_bars(jump_to_1525, t_1530_early).iloc[-1]['dt'].strftime('%H:%M'), '15:25')
        self.assertEqual(F.current_bars(jump_to_1525, t_1530_due).iloc[-1]['dt'].strftime('%H:%M'), '15:25')
        with self.assertRaisesRegex(ValueError, 'missing completed'):
            F.current_bars(frame('15:05'), t_1526)
        with self.assertRaisesRegex(ValueError, 'missing completed'):
            F.current_bars(only_1510, t_1530_due)
        st = M.fresh_state('2026-11-23')
        st['setups']['TEST:seed'] = dict(id='TEST:seed', symbol='TEST', phase='invalid')
        st['history_health'] = {'TEST': True}
        st['cursors']['TEST'] = only_1510.iloc[-1]['dt'].isoformat()
        st['feed_health'] = {'TEST': dict(ok=True, at=t_1520.isoformat(), latest=only_1510.iloc[-1]['dt'].isoformat())}
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, t_1526, dry_run=True)
            self.assertEqual(st['health_notice']['status'], 'healthy')
            st['cursors']['TEST'] = jump_to_1525.iloc[-1]['dt'].isoformat()
            st['feed_health']['TEST']['latest'] = jump_to_1525.iloc[-1]['dt'].isoformat()
            R.notify_health(st, t_1536, dry_run=True)
            self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_stale_nonempty_1d_and_symbol_backoff_do_not_double_range_requests(self):
        with patch.object(F.feeds, 'fetch_bars_yahoo', return_value=frame('09:30')) as fetch:
            with self.assertRaisesRegex(ValueError, 'stale'):
                F.fetch('TEST', None, NOW)
        self.assertEqual(fetch.call_count, 1)
        def backed_off(*args, **kwargs):
            kwargs['on_error'](dict(host='shared-m16-symbol', reason='empty response backoff',
                                    http_status=None, retry_after=120.0))
            return None
        with patch.object(F.feeds, 'fetch_bars_yahoo', side_effect=backed_off) as fetch:
            with self.assertRaisesRegex(ValueError, 'empty response backoff'):
                F.fetch('ICICIPRULI', None, NOW)
        self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
