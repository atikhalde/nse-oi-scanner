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
        self.assertEqual(len(st['outbox']), 1)
        self.assertTrue(next(iter(st['outbox'].values()))['awaiting_sector'])
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
        with patch.object(R.shared, 'preload', return_value=(0,0)), patch.object(R.shared, 'fetch_indices', return_value={}), patch.object(G, 'fetch_members', return_value={}):
            with self.assertRaises(ValueError): G.refresh(at('09:55:02'))

    def test_complete_worker_output_is_json_serializable(self):
        import json
        import pandas as pd
        import numpy as np
        from types import SimpleNamespace
        frame = pd.DataFrame([bar('09:50')])
        indices = list(R.shared.FM.OFFICIAL_TICKERS.values())+[R.shared.FM.NIFTY_TICKER,R.shared.FM.VIX_TICKER]
        members = {s for group in R.shared.MEMBERS.values() for s in group}
        board = SimpleNamespace(sectors={sec: dict(persist_bull=np.float64(75),persist_bear=np.float64(25)) for sec in set(R.shared.FM.OFFICIAL_TICKERS)|set(R.shared.MEMBERS)})
        with patch.object(R.shared,'preload',return_value=(len(members),len(set(indices)))), patch.object(R.shared,'fetch_indices',return_value={s:frame for s in indices}), patch.object(
                G,'fetch_members',return_value={s:frame for s in members}), patch.object(
                R.shared,'symbol_history',return_value={'ok':True}), patch.object(
                R.shared,'build_board',return_value=(board,'complete')):
            result=G.refresh(at('09:55:02'))
        json.dumps(result,allow_nan=False)
        self.assertEqual(len(result['sectors']),30)

    def test_failure_update_remains_available_with_missing_board(self):
        st=state()
        self.assertTrue(R.sector_send_ok(st,dict(kind='CLOSE_FAILED'),at('10:00:01')))

    def pending_close(self):
        st, _ = armed()
        st['sector_gate_required'] = True
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=111, l=108, c=110.5, e9=109, e10=108), at('09:55:02'))
        return st

    def test_dry_run_close_waits_for_board_then_delivers_once(self):
        st = self.pending_close()
        with patch.object(R.time, 'monotonic', return_value=0), patch.object(R.tg, 'send_message_to') as send:
            R.flush(st, at('09:55:03'), dry_run=True)
            self.assertEqual(next(iter(st['outbox'].values()))['status'], 'pending')
            st['sector_snapshots'] = state()['sector_snapshots']
            st['sector_snapshots'][0]['observed'] = at('09:55:05').isoformat()
            R.flush(st, at('09:55:06'), dry_run=True)
            R.flush(st, at('09:55:07'), dry_run=True)
            send.assert_not_called()
        self.assertEqual(next(iter(st['outbox'].values()))['status'], 'dry_run')
        self.assertTrue(st['events'][0]['live'])
        self.assertEqual(st['events'][0]['sector_gate_at_detection']['status'], 'unknown')
        self.assertEqual(st['events'][0]['sector_eligible_at'], at('09:55:06').isoformat())

    def test_pending_close_survives_json_restart(self):
        import json
        st = json.loads(json.dumps(self.pending_close()))
        st['sector_snapshots'] = state()['sector_snapshots']
        with patch.object(R.time, 'monotonic', return_value=0):
            R.flush(st, at('09:55:05'), dry_run=True)
        self.assertEqual(st['events'][0]['sector_gate']['status'], 'pass')
        self.assertTrue(st['events'][0]['live'])

    def test_late_board_cannot_resurrect_expired_close(self):
        st = self.pending_close()
        st['sector_snapshots'] = state()['sector_snapshots']
        with patch.object(R.time, 'monotonic', return_value=0), patch.object(R.tg,'send_message_to') as send:
            R.flush(st, at('09:55:33'), dry_run=True)
            send.assert_not_called()
        self.assertEqual(next(iter(st['outbox'].values()))['status'], 'expired_unsent')
        self.assertFalse(st['events'][0]['live'])

    def test_disqualified_board_rejects_waiting_close(self):
        st = self.pending_close()
        st['sector_snapshots'] = state(69)['sector_snapshots']
        with patch.object(R.time,'monotonic', return_value=0):
            R.flush(st, at('09:55:05'), dry_run=True)
        self.assertEqual(next(iter(st['outbox'].values()))['status'], 'sector_rejected')
        self.assertFalse(st['events'][0]['live'])

    def test_old_backfill_is_never_queued_for_sector_retry(self):
        st, key = armed()
        st['sector_gate_required'] = True
        M.emit(st, st['setups'][key], 'CLOSE_ONLY', at('09:50'), 111, at('12:00'), False)
        self.assertFalse(st['outbox'])

    def test_unknown_running_break_is_never_backdated(self):
        st, key = armed()
        st['sector_gate_required'] = True
        M.emit(st, st['setups'][key], 'RUNNING_BREAK', at('09:50'),111,at('09:52'),True)
        self.assertFalse(st['outbox'])
        self.assertFalse(st['events'][0]['live'])

    def test_sector_baskets_cannot_use_dhan_fallback(self):
        import pandas as pd
        with patch.object(R.shared.feeds,'fetch_bars_yahoo',return_value=pd.DataFrame([bar('09:50')])) as yahoo, patch.object(
                R.shared.feeds,'fetch_today',side_effect=AssertionError('Dhan-capable path forbidden')):
            bars=G.fetch_members(['AXISBANK'],at('09:55:02'))
        self.assertIn('AXISBANK',bars)
        yahoo.assert_called_once()

    def test_board_delayed_close_sends_to_both_bots_once_without_network(self):
        st=self.pending_close()
        st['sector_snapshots']=state()['sector_snapshots']
        with patch.object(R.time,'monotonic',return_value=0), patch.object(R,'save'), patch.object(
                R,'telegram_targets',return_value=[('test-token-one','chat-one'),('test-token-two','chat-two')]), patch.object(
                R.tg,'send_message_to',return_value=True) as send:
            R.flush(st,at('09:55:06'))
            R.flush(st,at('09:55:07'))
        self.assertEqual(send.call_count,2)
        self.assertEqual(next(iter(st['outbox'].values()))['status'],'sent')

    def test_sector_probe_after_hours_checks_final_close_without_state_or_bot(self):
        with patch.object(R.shared,'now_ist',return_value=at('21:00')), patch.object(
                G,'refresh',return_value=dict(close=at('15:30').isoformat(),sectors={str(i):{} for i in range(30)},meta='complete')) as refresh, patch.object(
                R,'save') as save, patch.object(R.tg,'send_message_to') as send:
            self.assertTrue(R.sector_probe())
        self.assertEqual(refresh.call_args.args[0],at('15:30'))
        save.assert_not_called(); send.assert_not_called()

    def test_sector_probe_failure_is_explicit(self):
        with patch.object(R.shared,'now_ist',return_value=at('10:00')), patch.object(G,'refresh',side_effect=ValueError('TEST latest=none')):
            self.assertFalse(R.sector_probe())

    def test_missing_sector_never_silently_promotes_known_third(self):
        st=state(71)
        st['sector_snapshots'][0]['missing_sectors']=['INSURANCE']
        verdict=G.decision(st,'AXISBANK','BUY',at('09:55:02'))
        self.assertEqual(verdict['status'],'unknown')
        self.assertEqual(verdict['known_rank'],3)
        self.assertEqual(verdict['worst_rank'],4)
        st['sector_snapshots'][0]['sectors']['BANK']['persist_bull']=85
        verdict=G.decision(st,'AXISBANK','BUY',at('09:55:02'))
        self.assertEqual(verdict['status'],'pass')
        self.assertEqual(verdict['worst_rank'],3)

    def test_affected_sector_itself_is_unknown(self):
        st=state()
        st['sector_snapshots'][0]['missing_sectors']=['INSURANCE']
        self.assertEqual(G.decision(st,'ICICIPRULI','BUY',at('09:55:02'))['status'],'unknown')

    def test_guaranteed_pass_survives_every_possible_unknown_score(self):
        import copy
        for side,key in [('BUY','persist_bull'),('SELL','persist_bear')]:
            for known_score in (69,70,71,85,99):
                partial=state()
                partial['sector_snapshots'][0]['sectors']['BANK'][key]=known_score
                partial['sector_snapshots'][0]['missing_sectors']=['UNKNOWN']
                verdict=G.decision(partial,'AXISBANK',side,at('09:55:02'))
                if verdict['status']!='pass': continue
                for unknown_score in (0,70,71,85,99,100):
                    full=copy.deepcopy(partial)
                    full['sector_snapshots'][0]['missing_sectors']=[]
                    full['sector_snapshots'][0]['sectors']['UNKNOWN']=dict(persist_bull=unknown_score,persist_bear=unknown_score)
                    self.assertEqual(G.decision(full,'AXISBANK',side,at('09:55:02'))['status'],'pass')

    def test_missing_icicipruli_excludes_whole_insurance_not_entire_board(self):
        import pandas as pd
        from types import SimpleNamespace
        frame=pd.DataFrame([bar('09:50')])
        indices=list(R.shared.FM.OFFICIAL_TICKERS.values())+[R.shared.FM.NIFTY_TICKER,R.shared.FM.VIX_TICKER]
        members={s for group in R.shared.MEMBERS.values() for s in group}
        expected=(set(R.shared.FM.OFFICIAL_TICKERS)|set(R.shared.MEMBERS))-{'INSURANCE'}
        def build(close,today,bars,idx,history):
            self.assertFalse(set(R.shared.MEMBERS['INSURANCE']) & set(bars))
            R.shared._SYNTH_Y_CACHE[('INSURANCE',today)]={'test':'incomplete'}
            return SimpleNamespace(sectors={sec:dict(persist_bull=80,persist_bear=20) for sec in expected}), '17 official / 12 synthetic'
        with patch.object(R.shared,'preload',return_value=(len(members),len(set(indices)))), patch.object(
                R.shared,'fetch_indices',return_value={s:frame for s in indices}), patch.object(
                G,'fetch_members',return_value={s:frame for s in members if s!='ICICIPRULI'}), patch.object(
                R.shared,'symbol_history',return_value={'ok':True}), patch.object(R.shared,'build_board',side_effect=build):
            result=G.refresh(at('09:55:02'))
        self.assertEqual(len(result['sectors']),29)
        self.assertEqual(result['missing_members'],['ICICIPRULI'])
        self.assertEqual(result['missing_sectors'],['INSURANCE'])
        self.assertFalse(result['complete'])
        self.assertNotIn(('INSURANCE',at('09:55:02').strftime('%Y-%m-%d')),R.shared._SYNTH_Y_CACHE)

    def test_real_engine_other_persistence_scores_do_not_depend_on_missing_sector(self):
        import numpy as np
        n=18
        def inputs(offset):
            c=np.linspace(100+offset,105+offset,n)
            return dict(o=c-.1,h=c+.5,l=c-.5,c=c,c_full=np.concatenate([np.linspace(98,100,30),c]),
                        prev_close=100.,day_open=c[0],or_high=max(c[:6])+.5,or_low=min(c[:6])-.5)
        all_inputs={s:inputs(i) for i,s in enumerate(['BANK','FMCG','PHA','INSURANCE'])}
        full=R.shared.FM.compute_board(all_inputs,dict(n_intra=np.linspace(0,1,n)),15.,90)['sectors']
        partial=R.shared.FM.compute_board({s:v for s,v in all_inputs.items() if s!='INSURANCE'},dict(n_intra=np.linspace(0,1,n)),15.,90)['sectors']
        for s in partial:
            self.assertEqual(partial[s]['persist_bull'],full[s]['persist_bull'])
            self.assertEqual(partial[s]['persist_bear'],full[s]['persist_bear'])
