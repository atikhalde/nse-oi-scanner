"""Offline M16 state machine, timing, isolation and delivery regressions."""
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import m16_model as M
import m16_runner as R

DAY = "2026-11-20"


def at(t):
    return pd.Timestamp(f"{DAY} {t}", tz=M.IST)


def bar(t, o=104, h=105, l=103, c=104, e9=104, e10=104):
    return dict(dt=at(t).isoformat(), open=o, high=h, low=l, close=c, volume=100,
                ema9=e9, ema10=e10)


def invert(b):
    r = dict(b)
    r.update(open=200-b['open'], high=200-b['low'], low=200-b['high'], close=200-b['close'],
             ema9=200-b['ema9'], ema10=200-b['ema10'])
    return r


def armed(sell=False):
    st = M.fresh_state(DAY)
    first = bar("09:30", h=105, l=100)
    middle = bar("09:35", o=104, h=107, l=104, c=106.5, e9=105.5, e10=105)
    peak = bar("09:40", o=106, h=110, l=105, c=109, e9=107.5, e10=107)
    pullback = bar("09:45", o=109, h=109, l=107, c=108, e9=108, e10=107.8)
    if sell:
        first, middle, peak, pullback = map(invert, (first, middle, peak, pullback))
    key = M.seed(st, "AXISBANK", 211 if sell else 111, "SELL-EX11" if sell else "BUY-EX11",
                 first, pd.DataFrame([first]))
    M.closed_bar(st, "AXISBANK", middle, at("09:40:01"))
    M.closed_bar(st, "AXISBANK", peak, at("09:45:01"))
    M.closed_bar(st, "AXISBANK", pullback, at("09:50:01"))
    return st, key


class ContinuationTests(unittest.TestCase):
    def test_fib_and_sell_symmetry_two_alerts(self):
        for sell in (False, True):
            st, key = armed(sell)
            self.assertEqual(st['setups'][key]['phase'], 'armed')
            self.assertIn('FIB', st['setups'][key]['pullback'])
            b = bar("09:50", o=108, h=111, l=108, c=110.1)
            if sell:
                b = invert(b)
            M.running_bar(st, 'AXISBANK', b, at('09:52:00'))
            M.running_bar(st, 'AXISBANK', b, at('09:52:01'))
            self.assertEqual(len(st['events']), 1)
            M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
            self.assertEqual([e['kind'] for e in st['events']], ['RUNNING_BREAK', 'CLOSE_CONFIRMED'])
            self.assertEqual(st['setups'][key]['phase'], 'confirmed')
            self.assertEqual(len(st['outbox']), 2)

    def test_ema9_or_ema10(self):
        for n in (9, 10):
            st, key = armed()
            s = st['setups'][key]
            s['phase'] = 'rally'
            b = bar('09:50', o=109.3, h=109.5, l=109, c=109.2, e9=108, e10=108)
            b[f'ema{n}'] = 109.1
            M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
            self.assertEqual(s['phase'], 'armed')
            self.assertEqual(s['pullback'], f'EMA{n}')

    def test_failed_close_is_not_confirmed_entry(self):
        st, key = armed()
        b = bar('09:50', h=111, l=108, c=110.5)
        M.running_bar(st, 'AXISBANK', b, at('09:52'))
        b['close'] = 109
        M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
        self.assertEqual(st['events'][-1]['kind'], 'CLOSE_FAILED')
        self.assertEqual(st['setups'][key]['phase'], 'failed')

    def test_duplicate_seed_same_bos_alerts_once(self):
        st, key = armed()
        second = copy.deepcopy(st['setups'][key])
        second['id'] += ':other-label'
        st['setups'][second['id']] = second
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        self.assertEqual(len(st['events']), 1)
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:55:01'))
        self.assertEqual(len(st['events']), 2)

    def test_deep_pullback_and_anchor_break_invalid(self):
        st, key = armed()
        st['setups'][key]['phase'] = 'rally'
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=109, l=104, c=108), at('09:55:01'))
        self.assertEqual(st['setups'][key]['phase'], 'invalid')
        st, key = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=99, c=111), at('09:52'))
        self.assertEqual(st['setups'][key]['phase'], 'invalid')
        self.assertFalse(st['events'])

    def test_sideways_cannot_arm_even_with_ema_touch(self):
        for sell in (False, True):
            st = M.fresh_state(DAY)
            first = bar('09:30', h=104.02, l=100, c=104, e9=103.9, e10=103.8)
            sequence = [bar('09:35', h=104.04, l=103.9, c=104.01, e9=103.91, e10=103.81),
                        bar('09:40', h=104.06, l=103.9, c=104.02, e9=103.92, e10=103.82),
                        bar('09:45', h=104.05, l=103.9, c=104.01, e9=104, e10=103.95)]
            if sell:
                first = invert(first)
                sequence = list(map(invert, sequence))
            key = M.seed(st, 'A', 211 if sell else 111, 'EX', first, pd.DataFrame([first]))
            for b in sequence:
                M.closed_bar(st, 'A', b, M.stamp(b['dt'])+pd.Timedelta(minutes=5, seconds=1))
            self.assertEqual(st['setups'][key]['phase'], 'rally')
            self.assertFalse(st['setups'][key]['rally_evidence']['ready'])

    def test_rally_requires_efficiency_and_both_ema_slopes(self):
        def trail(closes, slopes):
            return [dict(close=c, ema9=100+slopes*i, ema10=99+slopes*i) for i,c in enumerate(closes)]
        self.assertFalse(M.rally_metrics(trail([100,102,100.2,102.2], 1), 1)['ready'])
        self.assertFalse(M.rally_metrics(trail([100,101,102], -1), 1)['ready'])
        self.assertTrue(M.rally_metrics(trail([100,101,102], 1), 1)['ready'])

    def test_either_ema_close_violation_permanently_invalidates(self):
        for sell in (False, True):
            for n in (9,10):
                st, key = armed(sell)
                b = bar('09:50', o=108, h=109, l=107, c=107.9, e9=107.5, e10=107.5)
                b[f'ema{n}'] = 108
                if sell:
                    b = invert(b)
                M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
                self.assertEqual(st['setups'][key]['phase'], 'invalid')
                b = bar('09:55', h=112, l=108, c=111)
                if sell:
                    b = invert(b)
                M.running_bar(st, 'AXISBANK', b, at('09:56'))
                self.assertFalse(st['events'])

    def test_wick_and_equal_close_allowed_but_missing_candle_fails_closed(self):
        st, key = armed()
        M.closed_bar(st, 'AXISBANK', bar('09:50', o=108, h=109, l=107, c=108, e9=108, e10=107.9), at('09:55:01'))
        self.assertEqual(st['setups'][key]['phase'], 'armed')
        st, key = armed()
        M.closed_bar(st, 'AXISBANK', bar('09:55', h=112, l=108, c=111), at('10:00:01'))
        self.assertEqual(st['setups'][key]['phase'], 'invalid')
        self.assertIn('missing', st['setups'][key]['invalid_reason'])

    def test_breakout_close_through_ema_is_failed_confirmation(self):
        st, key = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111, e9=111.1, e10=110.5), at('09:55:01'))
        self.assertEqual(st['setups'][key]['phase'], 'failed')
        self.assertEqual(st['events'][-1]['kind'], 'CLOSE_FAILED')

    def test_wick_alone_no_running_alert(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=109), at('09:52'))
        self.assertFalse(st['events'])

    def test_close_only_is_not_fake_intrabar(self):
        st, _ = armed()
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:55:01'))
        self.assertEqual([e['kind'] for e in st['events']], ['CLOSE_ONLY'])

    def test_no_lookahead_or_old_running_bar(self):
        st, _ = armed()
        original = copy.deepcopy(st)
        b = bar('09:50', h=112, l=108, c=111)
        M.closed_bar(st, 'AXISBANK', b, at('09:52'))
        M.running_bar(st, 'AXISBANK', b, at('09:55:01'))
        self.assertEqual(st, original)

    def test_same_bar_peak_touch_not_armed(self):
        st, key = armed()
        s = st['setups'][key]
        s['phase'] = 'rally'
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=107, c=111), at('09:55:01'))
        self.assertEqual(s['phase'], 'rally')
        self.assertEqual(s['peak'], 112)

    def test_time_cutoffs_and_all_ex(self):
        for code in (80, 101, 110, 111, 112, 201, 219, 220, 280):
            st = M.fresh_state(DAY)
            b = bar('10:50')
            self.assertIsNotNone(M.seed(st, 'A', code, 'EX', b, pd.DataFrame([b])))
        for code in (90, 290, 0, 300):
            st = M.fresh_state(DAY)
            b = bar('10:50')
            self.assertIsNone(M.seed(st, 'A', code, 'PREVIEW', b, pd.DataFrame([b])))
        b = bar('10:55')  # confirmed at 11:00, not BEFORE 11:00
        self.assertIsNone(M.seed(M.fresh_state(DAY), 'A', 111, 'EX', b, pd.DataFrame([b])))
        st, key = armed()
        M.running_bar(st, 'AXISBANK', bar('12:25', h=112, l=108, c=111), at('12:27'))
        self.assertFalse(st['events'])  # would close at 12:30, outside confirmation window
        M.expire(st, at('12:30'))
        self.assertEqual(st['setups'][key]['phase'], 'expired')

    def test_historical_close_never_alerts(self):
        st, _ = armed()
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('10:10'))
        self.assertEqual(st['events'][-1]['kind'], 'CLOSE_ONLY')
        self.assertFalse(st['events'][-1]['live'])
        self.assertFalse(st['outbox'])

    def test_missing_trigger_close_not_confirmed_later(self):
        st, key = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        M.closed_bar(st, 'AXISBANK', bar('09:55', h=112, l=108, c=111), at('10:00:01'))
        self.assertEqual(st['events'][-1]['kind'], 'CLOSE_UNAVAILABLE')
        self.assertEqual(st['setups'][key]['phase'], 'failed')


class DirectOriginalBOSTests(unittest.TestCase):
    def pattern(self, sell=False):
        rows = [bar('09:25', h=105, l=100, c=104),
                bar('09:30', o=104, h=107, l=104, c=106.5, e9=105.5, e10=105),
                bar('09:35', o=106, h=110, l=105, c=109, e9=107.5, e10=107),
                bar('09:40', o=109, h=109, l=107, c=108, e9=108, e10=107.8),
                bar('09:45', o=108, h=112, l=108, c=111, e9=109, e10=108.7)]
        return list(map(invert, rows)) if sell else rows

    def test_original_signal_first_appears_on_bos_buy_and_sell(self):
        for sell in (False, True):
            rows = self.pattern(sell)
            st = M.fresh_state(DAY)
            key = M.seed(st, 'A', 212 if sell else 110, 'SELL-EX11' if sell else 'BUY-EX11', rows[-1], pd.DataFrame(rows))
            self.assertTrue(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:01')))
            self.assertEqual(st['setups'][key]['phase'], 'confirmed')
            self.assertEqual(st['events'][0]['kind'], 'DIRECT_BOS_CONFIRMED')
            self.assertEqual(st['events'][0]['seed'], rows[-1]['dt'])
            self.assertEqual(st['events'][0]['bos'], 90 if sell else 110)
            self.assertEqual(len(st['outbox']), 1)
            self.assertFalse(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:02')))
            self.assertEqual(len(st['events']), 1)

    def test_no_pullback_or_no_breakout_not_direct(self):
        rows = self.pattern()
        rows[3].update(open=109.1, high=109.9, low=109, close=109.5, ema9=108, ema10=107.8)
        st = M.fresh_state(DAY)
        key = M.seed(st, 'A', 110, 'EX', rows[-1], pd.DataFrame(rows))
        self.assertFalse(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:01')))
        rows = self.pattern()
        rows[-1]['close'] = 109.5
        st = M.fresh_state(DAY)
        key = M.seed(st, 'A', 110, 'EX', rows[-1], pd.DataFrame(rows))
        self.assertFalse(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:01')))

    def test_ema_violation_rejects_direct_and_replay_never_alerts(self):
        rows = self.pattern()
        rows[3]['ema9'] = 108.1
        st = M.fresh_state(DAY)
        key = M.seed(st, 'A', 110, 'EX', rows[-1], pd.DataFrame(rows))
        self.assertFalse(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:01')))
        rows = self.pattern()
        st = M.fresh_state(DAY)
        key = M.seed(st, 'A', 110, 'EX', rows[-1], pd.DataFrame(rows))
        self.assertTrue(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('10:00')))
        self.assertFalse(st['outbox'])
        self.assertFalse(st['events'][0]['live'])

    def test_existing_continuation_close_is_not_alerted_again(self):
        rows = self.pattern()
        st = M.fresh_state(DAY)
        first = M.seed(st, 'A', 101, 'BUY-EX', rows[0], pd.DataFrame(rows[:1]))
        for b in rows[1:]:
            M.closed_bar(st, 'A', b, M.stamp(b['dt'])+pd.Timedelta(minutes=5,seconds=1))
        self.assertEqual(st['setups'][first]['phase'], 'confirmed')
        key = M.seed(st, 'A', 110, 'BUY-EX11', rows[-1], pd.DataFrame(rows))
        self.assertTrue(M.direct_original_bos(st, key, rows[-1], pd.DataFrame(rows), at('09:50:01')))
        self.assertEqual(len(st['events']), 1)
        self.assertEqual(len(st['outbox']), 1)
        self.assertIn('coincident_original_signals', st['events'][0])

    def test_apply_integration_and_incomplete_or_future_context(self):
        rows = self.pattern()
        st = M.fresh_state(DAY)
        R.apply(st, 'A', [(rows[-1],110,'BUY-EX11',pd.DataFrame(rows).to_dict('list'))], at('09:50:01'))
        self.assertEqual(st['events'][0]['kind'], 'DIRECT_BOS_CONFIRMED')
        self.assertIn('directly on BOS', st['signal_audit'][0]['reason'])
        st = M.fresh_state(DAY)
        key = M.seed(st, 'A',110,'EX',rows[-1],pd.DataFrame(rows))
        self.assertFalse(M.direct_original_bos(st,key,rows[-1],pd.DataFrame(rows),at('09:48')))
        self.assertFalse(M.direct_original_bos(st,key,rows[-1],pd.DataFrame(rows[-3:]),at('09:50:01')))
        self.assertFalse(M.direct_original_bos(st,key,rows[-1],pd.DataFrame({'low':[100]}),at('09:50:01')))


class RunnerTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def test_dedicated_credentials_only(self):
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'shared', 'TELEGRAM_CHAT_ID': 'shared'}, clear=True):
            with self.assertRaises(RuntimeError):
                R.credentials()
        with patch.dict(os.environ, {'M16_BOT_TOKEN': 'own', 'M16_CHAT_ID': 'ownchat'}, clear=True):
            self.assertEqual(R.credentials(), ('own', 'ownchat'))

    def test_delivery_restart_dedupe_failure_and_state_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, 'STATE', Path(tmp)/'state16.json'), patch.dict(
            os.environ, {'M16_BOT_TOKEN': 'own', 'M16_CHAT_ID': 'ownchat'}, clear=True):
            st, _ = armed()
            M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
            R.save(st)
            st = R.load(DAY)
            with patch.object(R.tg, 'send_message_to', return_value=True) as send:
                R.flush(st, at('09:52:01'))
                R.flush(st, at('09:52:02'))
                self.assertEqual(send.call_count, 1)
                self.assertEqual(send.call_args.args[:2], ('own', 'ownchat'))
            self.assertEqual(list(Path(tmp).iterdir()), [Path(tmp)/'state16.json'])
            self.assertFalse(R.load('2026-11-23')['setups'])
            R.STATE.write_text('{broken')
            with self.assertRaises(json.JSONDecodeError):
                R.load(DAY)

    def test_failed_send_retry_and_stale_expiry(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        with patch.object(R, 'save'), patch.object(R, 'credentials', return_value=('own', 'chat')), patch.object(
            R.tg, 'send_message_to', side_effect=[False, True]) as send:
            R.flush(st, at('09:52:01'))
            R.flush(st, at('09:52:05'))
            self.assertEqual(send.call_count, 2)
        list(st['outbox'].values())[0]['status'] = 'pending'
        with patch.object(R.tg, 'send_message_to') as send:
            R.flush(st, at('09:56'))
            send.assert_not_called()
        self.assertEqual(list(st['outbox'].values())[0]['status'], 'expired_unsent')

    def test_dry_run_no_writes_or_sends(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, 'STATE', Path(tmp)/'state16.json'), patch.object(
            R.tg, 'send_message_to') as send, patch.object(R.tg, 'send_document_to') as doc:
            R.save(st, True)
            R.flush(st, at('09:52:01'), True)
            R.finish(st, True)
            self.assertFalse(list(Path(tmp).iterdir()))
            send.assert_not_called()
            doc.assert_not_called()

    def test_running_send_is_expired_at_exact_candle_close(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:54:59'))
        event = st['events'][-1]
        self.assertTrue(R.event_fresh(event, at('09:54:59')))
        self.assertFalse(R.event_fresh(event, at('09:55:00')))
        self.assertFalse(R.event_fresh(event, at('09:55:01')))
        with patch.object(R.tg, 'send_message_to') as send:
            R.flush(st, at('09:55:00'))
            send.assert_not_called()

    def test_prior_close_must_be_processed_before_forming_break(self):
        st, _ = armed()
        st['cursors']['AXISBANK'] = at('09:45').isoformat()
        current = R.normalize(pd.DataFrame([bar('09:55', o=111, h=112, l=110, c=111)]), DAY)
        R.observe_running(st, 'AXISBANK', current, at('09:56'))
        self.assertFalse(st['events'])
        # Once the actual previous candle is processed, it is a close-only
        # event, not a later fictitious running crossing in the next candle.
        prior = bar('09:50', o=108, h=112, l=108, c=111)
        R.apply(st, 'AXISBANK', [(prior, None, '', {})], at('09:55:01'))
        R.observe_running(st, 'AXISBANK', current, at('09:56'))
        self.assertEqual([e['kind'] for e in st['events']], ['CLOSE_ONLY'])

    def test_all_ex_enabled_and_causal_seed_worker(self):
        rows = pd.DataFrame([bar('09:30'), bar('09:35'), bar('11:00')])
        rows = R.normalize(rows, DAY)
        prefixes = []
        def engine(frame, params):
            self.assertTrue(params.enable_buy_ex10)
            self.assertTrue(params.enable_buy_ex11)
            prefixes.append(frame.index[-1])
            return pd.DataFrame({'scan_code': [111], 'scan_name': ['BUY-EX11']}, index=[frame.index[-1]])
        with patch.object(R.scanner, 'run_symbol', side_effect=engine):
            sym, output = R.analyze(('A', rows, {'ok': True, 'eng_hist': None}, None, at('11:05:01')))
        self.assertEqual(len(prefixes), 2)
        self.assertEqual(len(output), 3)
        self.assertIsNone(output[-1][1])
        st = M.fresh_state(DAY)
        R.apply(st, sym, output, at('11:05:01'))
        self.assertEqual(len(st['setups']), 2)
        R.apply(st, sym, output, at('11:05:01'))
        self.assertEqual(len(st['setups']), 2)

    def test_axisbank_reference_offline_replay(self):
        with patch.object(R.tg, 'send_message_to') as msg, patch.object(R.shared, 'fetch_symbols') as feed:
            result = R.replay(DAY, ['AXISBANK'])
            msg.assert_not_called()
            feed.assert_not_called()
        seeds = list(result['setups'].values())
        self.assertTrue(any('09:30:00' in s['signal_bar'] for s in seeds))
        event = next(e for e in result['events'] if e['kind'] == 'CLOSE_ONLY')
        self.assertIn('10:35:00', event['bar'])
        self.assertAlmostEqual(event['bos'], 1394., places=1)
        self.assertFalse(result['outbox'])

    def run_snapshot_session(self, refresh_after_close):
        from concurrent.futures import Future
        class ImmediateExecutor:
            def __init__(self, **kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f
        st, _ = armed()
        st['cursors']['AXISBANK'] = at('09:45').isoformat()
        clock, elapsed = [at('09:52')], [0.]
        steps = iter([at('09:55:01'), at('09:55:02'), at('09:55:03'), at('15:31:01')])
        candle = bar('09:50', o=108, h=112, l=108, c=111)
        frame = R.normalize(pd.DataFrame([bar('09:45', o=108, h=109, l=107, c=108), candle]), DAY)
        def fetch(sym):
            if not refresh_after_close and clock[0] >= at('09:55'):
                raise RuntimeError('fresh feed unavailable after close')
            return frame, clock[0]
        def analyze(job):
            self.assertGreaterEqual(M.stamp(job[-1]), at('09:55'))
            return job[0], [(candle, None, '', {})]
        def sleep(_):
            clock[0] = next(steps)
            elapsed[0] += 2.
        with patch.object(R.m16_sector, 'refresh', side_effect=ValueError('offline test')), patch.object(R.m16_sector, 'decision', return_value={'status': 'pass'}), patch.object(R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
            R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(R, 'timed_fetch', side_effect=fetch), patch.object(
            R, 'analyze', side_effect=analyze), patch.object(R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
            R.time, 'sleep', side_effect=sleep), patch.object(R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 1, dry_run=True)
        return st

    def test_live_session_monitors_and_confirms_without_batch_wait(self):
        st = self.run_snapshot_session(refresh_after_close=True)
        self.assertEqual([e['kind'] for e in st['events']], ['RUNNING_BREAK', 'CLOSE_CONFIRMED'])

    def test_request_started_before_close_is_not_final_when_response_returns_later(self):
        frame = R.normalize(pd.DataFrame([bar('09:45'), bar('09:50')]), DAY)
        with patch.object(R.shared, 'now_ist', side_effect=[at('09:54:59'), at('09:55:03')]), patch.object(
            R.m16_feed, 'fetch', return_value=(frame, {'provider': 'test'})):
            bars, watermark = R.timed_fetch('AXISBANK')
        self.assertEqual(watermark, at('09:54:59'))
        closed = R.shared._closed_bars(bars, watermark)
        self.assertEqual(closed.iloc[-1]['t'], '09:45')

    def test_discovery_retries_cached_preclose_snapshot_after_close(self):
        frame = R.normalize(pd.DataFrame([bar('09:45'), bar('09:50')]), DAY)
        cached = (frame, at('09:54:59'))
        self.assertTrue(R.needs_discovery_refresh(cached, at('09:50')))
        self.assertFalse(R.needs_discovery_refresh((frame, at('09:55:01')), at('09:50')))
        self.assertTrue(R.needs_discovery_refresh(None, at('09:50')))

    def test_slow_first_event_does_not_refresh_age_of_second_event(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        first = next(iter(st['outbox'].values()))
        second = copy.deepcopy(first)
        second['event']['id'] += ':second'
        st['outbox']['second'] = second
        elapsed = [0.]
        def send(*args):
            elapsed[0] += 40.
            return True
        with patch.object(R, 'save'), patch.object(R, 'telegram_targets', return_value=[('own', 'chat')]), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(
                R.tg, 'send_message_to', side_effect=send) as sender:
            R.flush(st, at('09:52:01'))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(second['status'], 'expired_unsent')

    def test_cached_forming_snapshot_cannot_be_promoted_to_closed(self):
        st = self.run_snapshot_session(refresh_after_close=False)
        self.assertEqual([e['kind'] for e in st['events']], ['RUNNING_BREAK'])
        self.assertEqual(st['cursors']['AXISBANK'], at('09:45').isoformat())
        self.assertTrue(any('fresh feed unavailable' in d.get('reason', '') for d in st['diagnostics']))

    def test_eod_txt_and_pdf_attachments_only(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, 'STATE', Path(tmp)/'state16.json'), patch.object(
            R, 'credentials', return_value=('own', 'chat')), patch.object(R.tg, 'send_message_to') as msg, patch.object(
            R.tg, 'send_document_to', return_value=True) as doc:
            st, _ = armed()
            R.finish(st)
            R.finish(st)
            msg.assert_not_called()
            self.assertEqual(doc.call_count, 2)
            self.assertTrue((Path(tmp)/f'audit_M16_{DAY}.pdf').read_bytes().startswith(b'%PDF-'))
            content = (Path(tmp)/f'activity_M16_{DAY}.txt').read_text()
            self.assertIn('AXISBANK', content)
            self.assertTrue(st['eod_done'])


class MirrorTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, dict(M16_BOT_TOKEN='primary-secret', M16_CHAT_ID='primary-chat',
                                         M16_BOT_TOKEN_2='mirror-secret', M16_CHAT_ID_2='mirror-chat'), clear=True)
        env.start()
        self.addCleanup(env.stop)

    def event_state(self):
        st, _ = armed()
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        return st

    def test_optional_config_and_duplicate_targets(self):
        self.assertEqual(len(R.telegram_targets()), 2)
        with patch.dict(os.environ, {'M16_CHAT_ID_2': ''}):
            self.assertEqual(len(R.telegram_targets()), 1)
        with patch.dict(os.environ, {'M16_BOT_TOKEN_2': 'primary-secret', 'M16_CHAT_ID_2': 'primary-chat'}):
            self.assertEqual(len(R.telegram_targets()), 1)
        with patch.dict(os.environ, {'M16_BOT_TOKEN': ''}):
            with self.assertRaises(RuntimeError):
                R.telegram_targets()

    def test_alert_both_targets_and_failed_mirror_only_retry(self):
        st = self.event_state()
        with patch.object(R, 'save'), patch.object(R.tg, 'send_message_to', side_effect=[True, False, True]) as send:
            R.flush(st, at('09:52:01'))
            self.assertEqual(send.call_count, 2)
            R.flush(st, at('09:52:05'))
            R.flush(st, at('09:52:06'))
            self.assertEqual([c.args[0] for c in send.call_args_list], ['primary-secret', 'mirror-secret', 'mirror-secret'])
        self.assertEqual(next(iter(st['outbox'].values()))['status'], 'sent')
        serialized = json.dumps(st)
        for secret in ('primary-secret', 'mirror-secret', 'primary-chat', 'mirror-chat'):
            self.assertNotIn(secret, serialized)

    def test_primary_exception_still_delivers_mirror(self):
        st = self.event_state()
        with patch.object(R, 'save'), patch.object(R.tg, 'send_message_to', side_effect=[RuntimeError('failure'), True]) as send:
            R.flush(st, at('09:52:01'))
            self.assertEqual(send.call_count, 2)
        deliveries = next(iter(st['outbox'].values()))['deliveries']
        self.assertEqual(deliveries[R.target_id('mirror-secret', 'mirror-chat')], 'sent')

    def test_uncertain_destination_does_not_block_other_target(self):
        st = self.event_state()
        item = next(iter(st['outbox'].values()))
        item['deliveries'] = {R.target_id('primary-secret', 'primary-chat'): 'sending'}
        with patch.object(R, 'save'), patch.object(R.tg, 'send_message_to', return_value=True) as send:
            R.flush(st, at('09:52:01'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], 'mirror-secret')

    def test_txt_pdf_both_targets_without_duplicate_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(R, 'STATE', Path(tmp)/'state16.json'), patch.object(
            R.tg, 'send_document_to', side_effect=[True, False, True, True, True]) as doc:
            st = self.event_state()
            R.finish(st)
            self.assertFalse(st['eod_done'])
            R.finish(st)
            R.finish(st)
            self.assertTrue(st['eod_done'])
            self.assertEqual([c.args[0] for c in doc.call_args_list],
                             ['primary-secret', 'mirror-secret', 'primary-secret', 'mirror-secret', 'mirror-secret'])

    def test_slow_primary_cannot_send_expired_running_alert_to_mirror(self):
        st = self.event_state()
        with patch.object(R, 'save'), patch.object(R.time, 'monotonic', side_effect=[0.,0.,0.,40.]), patch.object(
            R.tg, 'send_message_to', return_value=True) as send:
            R.flush(st, at('09:52:01'))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[0], 'primary-secret')
        ledger = next(iter(st['outbox'].values()))['deliveries']
        self.assertEqual(ledger[R.target_id('mirror-secret', 'mirror-chat')], 'expired_unsent')

    def test_self_test_checks_both_and_reports_any_failure(self):
        with patch.object(R.tg, 'send_message_to', side_effect=[False, True]) as send:
            self.assertFalse(R.test_alert())
            self.assertEqual(send.call_count, 2)
        with patch.object(R.tg, 'send_message_to') as send:
            self.assertTrue(R.test_alert(dry_run=True))
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
