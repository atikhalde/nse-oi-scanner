import unittest
from unittest.mock import patch
import m16_sector as G
import m16_model as M
import m16_runner as R
from test_m16_runner import armed, at, bar


def state(score=71):
    return dict(sector_gate_required=True, sector_snapshots=[dict(
        close=at('09:55').isoformat(), observed=at('09:55:01').isoformat(),
        sectors={'BANK': dict(persist_bull=score, persist_bear=10),
                 'A': dict(persist_bull=90, persist_bear=95),
                 'B': dict(persist_bull=80, persist_bear=85),
                 'C': dict(persist_bull=60, persist_bear=75)})])


class SectorTests(unittest.TestCase):
    def test_threshold_direction_and_rank(self):
        self.assertEqual(G.decision(state(), 'AXISBANK', 'BUY', at('09:55:02'))['status'], 'pass')
        self.assertEqual(G.decision(state(70), 'AXISBANK', 'BUY', at('09:55:02'))['status'], 'block')
        self.assertEqual(G.decision(state(59), 'AXISBANK', 'BUY', at('09:55:02'))['status'], 'block')
        self.assertEqual(G.decision(state(), 'AXISBANK', 'SELL', at('09:55:02'))['status'], 'block')

    def test_no_future_or_stale_board(self):
        self.assertEqual(G.decision(state(), 'AXISBANK', 'BUY', at('09:55:00'))['status'], 'unknown')
        self.assertEqual(G.decision(state(), 'AXISBANK', 'BUY', at('10:00:01'))['status'], 'unknown')
        self.assertEqual(G.decision(state(), 'AXISBANK', 'BUY', at('09:59:59'))['status'], 'pass')

    def test_unknown_sector_and_missing_board(self):
        self.assertEqual(G.decision(state(), 'MISSING', 'BUY', at('09:55:02'))['status'], 'unknown')
        self.assertEqual(G.decision(dict(sector_gate_required=True), 'AXISBANK', 'BUY', at('09:55:02'))['status'], 'unknown')

    def test_model_blocks_but_retains_technical_event(self):
        st, _ = armed()
        st['sector_gate_required'] = True
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=111, l=108, c=110.5, e9=109, e10=108), at('09:55:02'))
        self.assertEqual(len(st['events']), 1)
        self.assertFalse(st['outbox'])
        self.assertEqual(st['events'][0]['sector_gate']['status'], 'unknown')

    def test_model_pass_and_send_protection(self):
        st, _ = armed()
        st.update(state())
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=111, l=108, c=110.5, e9=109, e10=108), at('09:55:02'))
        self.assertEqual(len(st['outbox']), 1)
        e=st['events'][0]
        self.assertTrue(R.sector_send_ok(st,e,at('09:55:03')))
        self.assertFalse(R.sector_send_ok(st,e,at('10:00:01')))
        e.pop('sector_gate')
        self.assertFalse(R.sector_send_ok(st,e,at('09:55:03')))

    def test_worker_rejects_incomplete_board(self):
        with patch.object(R.shared, 'fetch_indices', return_value={}), patch.object(R.shared, 'fetch_symbols', return_value={}):
            with self.assertRaises(ValueError): G.refresh(at('09:55:02'))

    def test_complete_worker_output_is_json_serializable(self):
        import json
        import pandas as pd
        import numpy as np
        from types import SimpleNamespace
        frame = pd.DataFrame([bar('09:50')])
        indices = list(R.shared.FM.OFFICIAL_TICKERS.values())+[R.shared.FM.NIFTY_TICKER,R.shared.FM.VIX_TICKER]
        members = {s for group in R.shared.MEMBERS.values() for s in group}
        board = SimpleNamespace(sectors={str(i): dict(persist_bull=np.float64(75),persist_bear=np.float64(25)) for i in range(30)})
        with patch.object(R.shared,'fetch_indices',return_value={s:frame for s in indices}), patch.object(
                R.shared,'fetch_symbols',return_value={s:frame for s in members}), patch.object(
                R.shared,'symbol_history',return_value={'ok':True}), patch.object(
                R.shared,'build_board',return_value=(board,'complete')):
            result=G.refresh(at('09:55:02'))
        json.dumps(result,allow_nan=False)
        self.assertEqual(len(result['sectors']),30)

    def test_failure_update_remains_available_with_missing_board(self):
        st=state()
        self.assertTrue(R.sector_send_ok(st,dict(kind='CLOSE_FAILED'),at('10:00:01')))
