"""Offline full-day PDF, observation coverage and report delivery tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import m16_audit as A
import m16_model as M
import m16_runner as R
from test_m16_runner import DAY, armed, at, bar


class AuditTests(unittest.TestCase):
    def test_all_report_sections_and_raw_details(self):
        st, key = armed()
        st['signal_audit'] = [dict(symbol='AXISBANK', bar=at('09:30').isoformat(), code=111,
                                   signal='BUY-EX', decision='tracked', reason='test seed')]
        st['diagnostics'] = [dict(reason='provider unavailable <&>', at=at('14:00').isoformat())]
        st['diagnostics_omitted'] = 12
        st['transitions'] = [dict(phase='armed', setup=key, source='closed candle')]
        text = '\n'.join(title+'\n'+'\n'.join(lines) for title,lines in A.sections(st))
        for token in ('EMA9', 'efficiency', 'test seed', 'provider unavailable <&>', 'COMPLETE SAVED AUDIT', key,
                      '12', 'Missing candles', 'delivery'):
            self.assertIn(token, text)
        self.assertEqual(len(list(A.sections(st))), 12)

    def test_post_event_path_does_not_use_same_bar_wick(self):
        st = M.fresh_state(DAY)
        st['events'] = [dict(id='break1', symbol='A', kind='RUNNING_BREAK', side='BUY',
                              bar=at('09:50').isoformat(), price=100)]
        st['audit_bars'] = {'A': {at('09:50').isoformat(): bar('09:50', h=1000, l=1, c=100),
                                  at('09:55').isoformat(): bar('09:55', h=102, l=98, c=101)}}
        path = dict(A.sections(st))['8. Later price behavior - observations, NOT trade P&L']
        text = '\n'.join(path)
        self.assertIn('best +2.000%', text)
        self.assertIn('worst -2.000%', text)
        self.assertIn('last +1.000%', text)
        self.assertNotIn('900.000', text)
        st['events'][0]['side'] = 'SELL'
        text = '\n'.join(dict(A.sections(st))['8. Later price behavior - observations, NOT trade P&L'])
        self.assertIn('last -1.000%', text)

    def test_pdf_unicode_long_lines_and_empty_legacy_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            st, _ = armed()
            st['diagnostics'] = [dict(reason='M&M ₹ <> & '+('X'*10000))]
            path = A.build_pdf(st, Path(tmp)/'audit.pdf')
            self.assertTrue(path.read_bytes().startswith(b'%PDF-'))
            self.assertIn(b'%%EOF', path.read_bytes()[-100:])
            self.assertGreater(path.stat().st_size, 1000)
            path = A.build_pdf(M.fresh_state(DAY), Path(tmp)/'empty.pdf')
            self.assertTrue(path.exists())

    def test_late_day_prices_are_audited_without_new_seed_or_entry(self):
        st, key = armed()
        R.expire_setups(st, at('12:30'))
        self.assertEqual(st['setups'][key]['phase'], 'expired')
        late = bar('15:25', o=111, h=113, l=110, c=112, e9=111, e10=110.9)
        R.apply(st, 'AXISBANK', [(late, None, '', {})], at('15:30:01'))
        self.assertEqual(len(st['setups']), 1)
        self.assertFalse(st['outbox'])
        self.assertIn(late['dt'], st['audit_bars']['AXISBANK'])
        self.assertEqual(st['transitions'][0]['phase'], 'expired')

    def test_signal_candle_and_phase_journals_are_idempotent(self):
        st = M.fresh_state(DAY)
        b = bar('09:30', l=100)
        job = [(b, 111, 'BUY-EX12', {'low': [100], 'high': [105]})]
        R.apply(st, 'A', job, at('09:35:01'))
        R.apply(st, 'A', job, at('09:35:02'))
        self.assertEqual(len(st['signal_audit']), 1)
        self.assertEqual(len(st['transitions']), 1)
        self.assertEqual(len(st['audit_bars']['A']), 1)

    def test_diagnostic_retention_is_explicit_not_silent(self):
        st = M.fresh_state(DAY)
        st['diagnostics'] = [dict(reason='unavailable') for _ in range(2011)]
        R.trim_diagnostics(st)
        self.assertEqual(st['diagnostics_omitted'], 11)
        self.assertEqual(len(st['diagnostics']), 2000)
        R.trim_diagnostics(st)
        self.assertEqual(st['diagnostics_omitted'], 11)

    def test_pdf_failure_preserves_txt_then_retries_pdf_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, 'STATE', Path(tmp)/'state16.json'), patch.object(
            R, 'credentials', return_value=('own', 'chat')), patch.dict(R.os.environ, {}, clear=True), patch.object(
            R.tg, 'send_document_to', return_value=True) as doc:
            st, _ = armed()
            with patch.object(A, 'build_pdf', side_effect=RuntimeError('renderer unavailable')):
                R.finish(st)
            self.assertFalse(st['eod_done'])
            self.assertEqual(doc.call_count, 1)
            R.finish(st)
            self.assertTrue(st['eod_done'])
            self.assertEqual(doc.call_count, 2)
            self.assertTrue(doc.call_args.args[2].endswith('.pdf'))
            self.assertTrue(st['diagnostics'])

    def test_dormant_symbols_excluded_from_provider_summary(self):
        st = M.fresh_state(DAY)
        st['inactive_symbols'] = ['ICICIPRULI']
        st['feed_health'] = {
            'AXISBANK': dict(ok=True),
            'ICICIPRULI': dict(ok=False, reason='empty response'),
        }
        sec1 = '\n'.join(dict(A.sections(st))['1. Executive summary and coverage'])
        self.assertIn("{'usable': 1}", sec1)
        self.assertNotIn("'unusable'", sec1)
        self.assertIn("dormant excluded: ['ICICIPRULI']", sec1)

    def test_large_appendix_pdf_builds_fast_with_wide_unicode_lines(self):
        import time
        st = M.fresh_state(DAY)
        st['audit_bars'] = {
            f'SYM{i}': {f'2026-11-20T09:{15 + (j % 40):02d}:00+05:30': bar('09:30') for j in range(30)}
            for i in range(40)
        }
        st['diagnostics'].append(dict(reason='M&M ₹ <> & \t ' + ('W' * 600)))
        with tempfile.TemporaryDirectory() as tmp:
            t0 = time.monotonic()
            out = A.build_pdf(st, Path(tmp) / 'large.pdf')
            elapsed = time.monotonic() - t0
            raw = out.read_bytes()
            self.assertTrue(raw.startswith(b'%PDF-'))
            self.assertIn(b'%%EOF', raw[-256:])
            self.assertLess(elapsed, 15.0)


if __name__ == '__main__':
    unittest.main()
