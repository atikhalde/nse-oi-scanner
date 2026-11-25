"""Holiday guards must not disguise real feed outages as market closure."""
import unittest
from datetime import datetime
from unittest.mock import patch
import m16_calendar as C
import m16_model as M
import m16_runner as R


def at(day, time='10:00:00'):
    return datetime.fromisoformat(day+'T'+time).replace(tzinfo=C.IST)


class CalendarTests(unittest.TestCase):
    def test_reported_november_24_holiday(self):
        status, reason = C.session_status(at('2026-11-24'))
        self.assertEqual(status, 'closed')
        self.assertIn('Guru Nanak', reason)

    def test_adjacent_weekdays_remain_open(self):
        for day in ('2026-11-23', '2026-11-25'):
            self.assertEqual(C.session_status(at(day))[0], 'open')

    def test_trading_holidays_not_clearing_holidays(self):
        self.assertEqual(len(C.HOLIDAYS), 16)
        self.assertEqual(C.session_status(at('2026-01-15'))[0], 'closed')
        for day in ('2026-02-19', '2026-03-19', '2026-04-01', '2026-08-26'):
            self.assertEqual(C.session_status(at(day))[0], 'open')

    def test_weekend_and_unsupported_special_session(self):
        self.assertEqual(C.session_status(at('2026-11-22'))[0], 'closed')
        self.assertEqual(C.session_status(at('2026-11-08'))[0], 'closed')

    def test_unknown_year_is_not_assumed_holiday(self):
        self.assertEqual(C.session_status(at('2027-01-04'))[0], 'unknown')

    def test_timezone_is_ist_not_utc_date(self):
        clock = datetime.fromisoformat('2026-11-23T20:00:00+00:00')
        self.assertEqual(C.session_status(clock)[0], 'closed')

    def test_main_holiday_exits_without_credentials_feed_history_or_state(self):
        with patch('sys.argv', ['m16_runner.py', '--session']), patch.object(
                R.shared, 'now_ist', return_value=at('2026-11-24')), patch.object(
                R, 'credentials') as creds, patch.object(R, 'load') as load, patch.object(
                R.shared, 'symbol_history') as history, patch.object(R, 'timed_fetch') as feed:
            R.main()
        for call in (creds, load, history, feed):
            call.assert_not_called()

    def test_closed_day_has_no_degraded_notice_or_live_fetch(self):
        st = M.fresh_state('2026-11-24')
        with patch.object(R, 'deliver_targets') as send, patch.object(
                R.shared, 'now_ist', return_value=at(st['date'])), patch.object(
                R, 'ThreadPoolExecutor') as pool:
            R.notify_health(st, at(st['date']))
            R.live_session(st, {}, 1)
        send.assert_not_called()
        pool.assert_not_called()

    def test_open_day_missing_feed_still_warns(self):
        st = M.fresh_state('2026-11-25')
        with patch.object(R.shared.L, 'SYMS', ['TEST']):
            R.notify_health(st, at(st['date']), True)
        self.assertEqual(st['health_notice']['status'], 'degraded')

    def test_no_false_degradation_before_first_completed_candle_is_due(self):
        st = M.fresh_state('2026-11-25')
        with patch.object(R, 'deliver_targets') as send:
            for clock in ('09:16:00', '09:19:59', '09:20:29'):
                R.notify_health(st, at(st['date'], clock))
        send.assert_not_called()
        self.assertNotIn('health_notice', st)

    def test_probe_cli_does_not_load_or_modify_live_state(self):
        with patch('sys.argv', ['m16_runner.py', '--feed-probe']), patch.object(
                R, 'feed_probe', return_value=[]) as probe, patch.object(R, 'load') as load, patch.object(
                R, 'save') as save, patch.object(R, 'credentials') as creds:
            R.main()
        probe.assert_called_once()
        load.assert_not_called()
        save.assert_not_called()
        creds.assert_not_called()

    def test_supervised_session_restart_cap_prevents_another_session(self):
        st = M.fresh_state('2026-11-25')
        st['session_starts'] = 1
        with patch('sys.argv', ['m16_runner.py', '--session']), patch.dict(
                'os.environ', {'M16_MAX_SESSION_STARTS': '1'}), patch.object(
                R.shared, 'now_ist', return_value=at(st['date'])), patch.object(
                R, 'credentials'), patch.object(R, 'load', return_value=st), patch.object(
                R.shared.L, 'SYMS', []), patch.object(R, 'live_session') as live:
            with self.assertRaisesRegex(SystemExit, 'restart cap'):
                R.main()
        live.assert_not_called()

    def test_manual_bot_test_allowed_on_holiday(self):
        with patch('sys.argv', ['m16_runner.py', '--test-alert']), patch.object(
                R, 'test_alert', return_value=True) as send:
            R.main()
        send.assert_called_once_with(False)


class WorkflowCoverageTests(unittest.TestCase):
    def test_normal_morning_shift_covers_entire_entry_window(self):
        import re
        import yaml
        from pathlib import Path
        workflow = yaml.safe_load((Path(__file__).parent / '.github/workflows/16_live_m16.yml').read_text())
        job = workflow['jobs']['continuation']
        script = next(step['run'] for step in job['steps'] if step.get('name') == 'M16 dedicated session')
        runtime = int(re.search(r'--session --max-runtime-min (\d+)', script).group(1))
        # 09:00 to 12:35 is 215 minutes; leave room for persistence/artifacts.
        self.assertGreaterEqual(runtime, 215)
        self.assertGreaterEqual(job['timeout-minutes'] - runtime, 10)
        self.assertIn('dry-run) python -u m16_runner.py --once --dry-run', script)


if __name__ == '__main__':
    unittest.main()
