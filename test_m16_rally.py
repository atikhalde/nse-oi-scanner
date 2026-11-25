"""Pre-signal rally regressions; no movers filter."""
import unittest
import pandas as pd
import m16_model as M
from test_m16_runner import at, bar, DAY, armed

class RallyContextTests(unittest.TestCase):
    def test_pre_signal_rally_counts_without_arming_prior_pullback(self):
        st = M.fresh_state(DAY)
        context = pd.DataFrame([bar('09:20', c=100), bar('09:25', c=101, e9=104.2, e10=104.1),
                                bar('09:30', c=104, h=105, e9=104.3, e10=104.2)])
        key = M.seed(st, 'TEST', 111, 'BUY-EX', context.iloc[-1].to_dict(), context)
        # Keep seed closes above EMA for a valid fixture.
        self.assertEqual(st['setups'][key]['phase'], 'invalid')
        context.loc[2, ['close', 'high']] = [106, 107]
        st = M.fresh_state(DAY)
        key = M.seed(st, 'TEST', 111, 'BUY-EX', context.iloc[-1].to_dict(), context)
        self.assertTrue(st['setups'][key]['rally_evidence']['ready'])
        self.assertEqual(st['setups'][key]['phase'], 'rally')
        self.assertNotIn('bos', st['setups'][key])

    def test_dabur_original_label_then_pullback_then_bos(self):
        import json
        from pathlib import Path
        bars = json.loads((Path(__file__).parent/'tests/fixtures/m16_dabur_2026-11-25.json').read_text())['bars']
        st = M.fresh_state('2026-11-25')
        trail = []
        for b in bars:
            now = M.stamp(b['dt'])+pd.Timedelta(minutes=5, seconds=1)
            M.closed_bar(st, 'DABUR', b, now)
            trail.append(b)
            if b['dt'][11:16] == '09:35':
                key = M.seed(st, 'DABUR', 101, 'BUY-EX', b, pd.DataFrame(trail))
                self.assertEqual(st['setups'][key]['phase'], 'rally')
        self.assertEqual(len(st['events']), 1)
        self.assertEqual(st['events'][0]['bar'][11:16], '10:15')
        self.assertAlmostEqual(st['events'][0]['bos'], 413.3, places=2)

    def test_future_context_is_excluded(self):
        context = pd.DataFrame([bar('09:30'), bar('09:35', c=110, e9=108, e10=107)])
        st = M.fresh_state(DAY)
        key = M.seed(st, 'TEST', 111, 'BUY-EX', context.iloc[0].to_dict(), context)
        self.assertEqual(len(st['setups'][key]['rally_trail']), 1)
        self.assertEqual(st['setups'][key]['anchor'], context.iloc[0]['low'])

    def test_legacy_movers_state_does_not_block_normal_alert(self):
        st, _ = armed()
        st['movers_gate_required'] = True
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=111, l=108, c=110.5, e9=109, e10=108), at('09:55:01'))
        self.assertEqual(len(st['outbox']), 1)
