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
                'os.environ', {'M16_MAX_SESSION_STARTS': '1', 'M16_AUTOMATIC_SESSION': '1'}), patch.object(
                R.shared, 'now_ist', return_value=at(st['date'])), patch.object(
                R, 'credentials'), patch.object(R, 'load', return_value=st), patch.object(
                R.shared.L, 'SYMS', []), patch.object(R, 'save') as save, patch.object(R, 'live_session') as live:
            with self.assertRaisesRegex(SystemExit, 'restart cap'):
                R.main()
        live.assert_not_called()
        save.assert_not_called()

    def test_manual_recovery_cli_runs_at_cap_without_real_state_writes(self):
        st = M.fresh_state('2026-11-25')
        st['session_starts'] = 12
        with patch('sys.argv', ['m16_runner.py', '--session']), patch.dict(
                'os.environ', {'M16_MAX_SESSION_STARTS': '12', 'M16_AUTOMATIC_SESSION': '0'}), patch.object(
                R.shared, 'now_ist', return_value=at(st['date'])), patch.object(
                R, 'credentials'), patch.object(R, 'load', return_value=st), patch.object(
                R.shared.L, 'SYMS', []), patch.object(R, 'save') as save, patch.object(R, 'live_session') as live:
            R.main()
        live.assert_called_once()
        save.assert_called_once()
        self.assertEqual(st['automatic_session_starts'], 12)
        self.assertEqual(st['session_starts'], 13)

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

    def test_external_dispatches_do_not_cancel_in_progress_session_or_bypass_cap(self):
        import yaml
        from pathlib import Path
        workflow = yaml.safe_load((Path(__file__).parent / '.github/workflows/16_live_m16.yml').read_text())
        self.assertFalse(workflow['concurrency']['cancel-in-progress'])
        triggers = workflow.get('on') or workflow.get(True)
        inputs = triggers['workflow_dispatch']['inputs']
        self.assertFalse(inputs['manual_recovery']['default'])
        auto_expr = workflow['jobs']['continuation']['env']['M16_AUTOMATIC_SESSION']
        self.assertIn('inputs.manual_recovery', auto_expr)

    def test_workflow_uses_node24_upload_artifact_and_guards_eod_handoff(self):
        import yaml
        from pathlib import Path
        workflow = yaml.safe_load((Path(__file__).parent / '.github/workflows/16_live_m16.yml').read_text())
        steps = workflow['jobs']['continuation']['steps']
        upload = next(s for s in steps if s.get('name') == 'Upload M16 diagnostic snapshot even after failure')
        self.assertEqual(upload['uses'], 'actions/upload-artifact@v7')
        handoff = next(s for s in steps if 'Hand off supervised shift' in s.get('name', ''))
        self.assertIn('eod_done', handoff['run'])

    def test_pre_open_0855_window_and_readiness_check_mode_alignment(self):
        import yaml
        from pathlib import Path
        import pandas as pd

        workflow = yaml.safe_load((Path(__file__).parent / '.github/workflows/16_live_m16.yml').read_text())
        triggers = workflow.get('on') or workflow.get(True)
        options = triggers['workflow_dispatch']['inputs']['mode']['options']
        self.assertIn('readiness-check', options)
        steps = workflow['jobs']['continuation']['steps']
        preserve = next(s for s in steps if s.get('name') == 'Preserve M16 state and review')
        handoff = next(s for s in steps if 'Hand off supervised shift' in s.get('name', ''))
        self.assertIn("inputs.mode != 'readiness-check'", preserve['if'])
        self.assertIn("inputs.mode != 'readiness-check'", handoff['if'])

        pre_open = pd.Timestamp('2026-11-27 08:57:00', tz='Asia/Kolkata')
        too_early = pd.Timestamp('2026-11-27 08:54:00', tz='Asia/Kolkata')
        with patch('sys.argv', ['m16_runner.py', '--session', '--dry-run']), patch.object(
                R.shared, 'now_ist', return_value=pre_open), patch.object(
                R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
                R.shared, 'symbol_history', return_value={'ok': True, 'last_day': '2026-11-26'}), patch.object(
                R, 'load', return_value=R.model.fresh_state('2026-11-27')), patch.object(
                R, 'save'), patch.object(R, 'live_session') as live:
            R.main()
        live.assert_called_once()

        with patch('sys.argv', ['m16_runner.py', '--session', '--dry-run']), patch.object(
                R.shared, 'now_ist', return_value=too_early), patch.object(R, 'live_session') as live2:
            R.main()
        live2.assert_not_called()

    def test_schedule_and_gate_window_match_runner_pre_open_window(self):
        import re
        import yaml
        from pathlib import Path

        workflow_text = (Path(__file__).parent / '.github/workflows/16_live_m16.yml').read_text()
        workflow = yaml.safe_load(workflow_text)
        triggers = workflow.get('on') or workflow.get(True)
        crons = [item['cron'] for item in triggers['schedule']]
        # 25 3 UTC == 08:55 IST: the pre-open buffer must exist before the 09:20
        # first-close gate, otherwise the shift loses the quiet warm-up window.
        self.assertIn('25 3 * * 1-5', crons)
        self.assertIn('5 10 * * 1-5', crons)  # 15:35 IST EOD delivery retry
        gate = re.search(r'hhmm < "(\d{2}:\d{2})" or hhmm >= "(\d{2}:\d{2})"', workflow_text)
        self.assertIsNotNone(gate)
        self.assertEqual(gate.groups(), ('08:55', '15:40'))
        runner = (Path(__file__).parent / 'm16_runner.py').read_text()
        runner_gate = re.search(r'strftime\("%H:%M"\) < "(\d{2}:\d{2})" or now\.strftime\("%H:%M"\) >= "(\d{2}:\d{2})"', runner)
        self.assertIsNotNone(runner_gate)
        self.assertEqual(runner_gate.groups(), gate.groups())


if __name__ == '__main__':
    unittest.main()
