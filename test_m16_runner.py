"""Offline M16 state machine, timing, isolation and delivery regressions."""
import contextlib
import copy
import json
import csv
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import m16_model as M
import m16_runner as R

DAY = "2026-11-20"


@contextlib.contextmanager
def gate_mode(**env):
    """Pin the legacy strict sector gate for tests that describe it."""
    with patch.dict(os.environ, {'M16_SECTOR_MODE': 'gate', **env}):
        yield


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


GATE_PASS = dict(status='pass', sector='BANK', score=90.0, known_rank=1, worst_rank=1,
                 missing_sectors=[], slope15=2.4, boards_above70=2, close=at('09:55').isoformat(),
                 observed=at('09:55:20').isoformat(), top3=['BANK'])
GATE_BLOCK = dict(status='block', sector='BANK', score=41.0, known_rank=9, worst_rank=10,
                  missing_sectors=[], slope15=-2.6, boards_above70=5, close=at('09:55').isoformat(),
                  observed=at('09:55:20').isoformat())
GATE_UNKNOWN = dict(status='unknown', reason='sector board stale')


class AdvisoryAndDuplicateTests(unittest.TestCase):
    """Advisory board, one alert per candle, highlighted symbol."""

    def emitted(self, kind='RUNNING_BREAK', gate=None, price=111.0, now='09:52', env=None):
        st, key = armed()
        st['sector_gate_required'] = True
        with patch.dict(os.environ, env or {}), patch.object(
                R.m16_sector, 'decision', return_value=dict(gate if gate is not None else GATE_PASS)):
            M.emit(st, st['setups'][key], kind, at('09:50'), price, at(now), True)
        return st

    def test_blocked_board_still_emits_in_advisory_mode(self):
        st = self.emitted(gate=GATE_BLOCK, now='09:52')
        self.assertEqual(len(st['outbox']), 1)
        event = st['events'][0]
        self.assertTrue(event['live'])
        self.assertEqual(event['sector_mode'], 'advisory')
        self.assertEqual(event['sector_gate']['status'], 'block')

    def test_gate_mode_still_blocks_a_qualified_sector(self):
        with gate_mode():
            st = self.emitted(gate=GATE_BLOCK)
        self.assertEqual(st['outbox'], {})
        self.assertFalse(st['events'][0]['live'])

    @staticmethod
    def item_for(st, kind):
        return next(v for v in st['outbox'].values() if v['event']['kind'] == kind)

    def test_duplicate_close_is_suppressed_after_a_delivered_running_alert(self):
        st = self.emitted()
        next(iter(st['outbox'].values()))['status'] = 'sent'
        with patch.object(R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][next(iter(st['setups']))], 'CLOSE_CONFIRMED', at('09:50'), 111.4, at('09:55:02'), True)
        item = self.item_for(st, 'CLOSE_CONFIRMED')
        self.assertEqual(item['status'], 'duplicate_suppressed')
        self.assertEqual(item['event']['duplicate_of'], st['events'][0]['id'])
        self.assertFalse(item['event']['live'])

    def test_duplicate_is_delivered_when_the_first_alert_failed(self):
        st = self.emitted()
        next(iter(st['outbox'].values()))['status'] = 'failed'
        with patch.object(R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][next(iter(st['setups']))], 'CLOSE_CONFIRMED', at('09:50'), 111.4, at('09:55:02'), True)
        item = self.item_for(st, 'CLOSE_CONFIRMED')
        self.assertEqual(item['status'], 'pending')
        self.assertNotIn('duplicate_of', item['event'])

    def test_pending_twin_suppresses_the_second_alert_of_one_candle(self):
        st = self.emitted()                       # RUNNING_BREAK stays pending
        with patch.object(R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][next(iter(st['setups']))], 'CLOSE_CONFIRMED', at('09:50'), 111.4, at('09:55:02'), True)
        self.assertEqual(self.item_for(st, 'CLOSE_CONFIRMED')['status'], 'duplicate_suppressed')

    def test_suppressed_duplicate_is_promoted_when_its_twin_never_goes_out(self):
        st = self.emitted()
        with patch.object(R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][next(iter(st['setups']))], 'CLOSE_CONFIRMED', at('09:50'), 111.4, at('09:55:02'), True)
        running = self.item_for(st, 'RUNNING_BREAK')
        running['status'] = 'failed'
        with patch.object(R, 'save'), patch.object(R, 'telegram_targets', return_value=[('t', 'c')]), patch.object(
                R.tg, 'send_message_to', return_value=True) as send, patch.object(
                R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            R.flush(st, at('09:55:05'))
        self.assertEqual(send.call_count, 1)      # the promoted close alert, not twice
        self.assertEqual(self.item_for(st, 'CLOSE_CONFIRMED')['status'], 'sent')
        self.assertEqual(self.item_for(st, 'CLOSE_CONFIRMED')['promoted_from_duplicate'], 'failed')
        self.assertTrue(any('never went out' in str(d.get('reason')) for d in st['diagnostics']))

    def test_risk_update_is_never_suppressed(self):
        st = self.emitted()
        next(iter(st['outbox'].values()))['status'] = 'sent'
        with patch.object(R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][next(iter(st['setups']))], 'CLOSE_FAILED', at('09:50'), 109.0, at('09:55:02'), True)
        self.assertEqual(len(st['outbox']), 2)
        self.assertNotEqual(self.item_for(st, 'CLOSE_FAILED')['status'], 'duplicate_suppressed')

    def test_suppression_can_be_switched_off(self):
        st, key = armed()
        st['sector_gate_required'] = True
        with patch.dict(os.environ, {'M16_SUPPRESS_DUPLICATES': '0'}), patch.object(
                R.m16_sector, 'decision', return_value=dict(GATE_PASS)):
            M.emit(st, st['setups'][key], 'RUNNING_BREAK', at('09:50'), 111.0, at('09:52'), True)
            next(iter(st['outbox'].values()))['status'] = 'sent'
            M.emit(st, st['setups'][key], 'CLOSE_CONFIRMED', at('09:50'), 111.4, at('09:55:02'), True)
        self.assertEqual(len(st['outbox']), 2)
        self.assertTrue(all(v['status'] in {'pending', 'sent'} for v in st['outbox'].values()))

    def test_alert_highlights_the_symbol_and_carries_the_context(self):
        st = self.emitted(gate=GATE_BLOCK, price=111.2)
        event = st['events'][0]
        text = R.alert_text(event)
        self.assertIn('Sector BANK 41.00 · 15m -2.6 ↓ · rank 9 · 5 sectors above 70', text)
        self.assertIn('below the old gate — advisory only', text)
        self.assertIn('Sector score is falling', text)
        self.assertIn('Entry +1.09% beyond BOS', text)
        self.assertIn('tier B', text)
        markup = R.alert_text(event, markup=True)
        self.assertIn('<b>AXISBANK</b> · BUY @ 111.20', markup)
        self.assertNotIn('&lt;b&gt;', markup)

    def test_symbol_with_an_ampersand_is_escaped_inside_the_bold_tag(self):
        st = self.emitted()
        event = dict(st['events'][0])
        event['symbol'] = 'M&M'
        markup = R.alert_text(event, markup=True)
        self.assertIn('<b>M&amp;M</b> · BUY @', markup)

    def test_extended_entry_and_tier_a_are_labelled(self):
        st = self.emitted(kind='CLOSE_CONFIRMED', price=112.0, now='09:55:02')
        event = dict(st['events'][0])
        event['signal'] = 'BUY-EX17'
        event['bar'] = at('10:30').isoformat()
        text = R.alert_text(event)
        self.assertIn('EXTENDED — a retest of BOS is the better entry', text)
        self.assertIn('tier A', text)
        self.assertNotIn('early window', text)

    def test_ema_undercut_is_reported_in_the_alert(self):
        st = self.emitted()
        event = dict(st['events'][0])
        event['ema_undercut'] = 0.19
        self.assertIn('undercut EMA9/10 by 0.19%', R.alert_text(event))


class BasketMembershipTests(unittest.TestCase):
    """BEML joins the DEF group without touching any sector score."""

    def test_beml_is_in_the_defence_group(self):
        rows = {r['symbol']: r['sector'] for r in csv.DictReader((R.ROOT / 'fno_sector_map.csv').open())}
        self.assertEqual(rows.get('BEML'), 'DEF')
        self.assertEqual(R.shared.SECTOR_OF.get('BEML'), 'DEF')
        self.assertIn('BEML', R.shared.SEC_STOCKS['DEF'])
        self.assertEqual(sorted(R.shared.SEC_STOCKS['DEF']),
                         ['BDL', 'BEL', 'BEML', 'COCHINSHIP', 'HAL', 'MAZDOCK', 'SOLARINDS'])

    def test_def_is_an_index_sector_so_no_synthetic_basket_changes(self):
        self.assertEqual(R.shared.FM.OFFICIAL_TICKERS.get('DEF'), 'NIFTY_IND_DEFENCE.NS')
        self.assertNotIn('DEF', R.shared.MEMBERS)          # score = published index
        self.assertEqual(len(R.shared.MEMBERS), 13)

    def test_beml_is_still_scanned_by_m16(self):
        self.assertIn(('BEML', 'DEF'), R.extra_universe())
        self.assertNotIn('BEML', {r['symbol'] for r in csv.DictReader((R.ROOT / 'fno_universe.csv').open())})

    def test_m6_never_fetches_a_basket_name_it_cannot_enter(self):
        cache = {'BEL': {}, 'HAL': {}}
        self.assertEqual(R.shared.entry_candidates({'DEF': ['BUY']}, cache), ['BEL', 'HAL'])
        self.assertNotIn('BEML', R.shared.entry_candidates({'DEF': ['BUY']}, cache))
        self.assertEqual(R.shared.entry_candidates({}, cache), [])


class ExtraUniverseTests(unittest.TestCase):
    def test_extra_universe_is_parsed_and_installed_without_touching_baskets(self):
        rows = [('GRSE', 'DEF'), ('PARAS', 'DEF')]
        for sym in ('GRSE', 'PARAS'):          # main() may already have installed them
            if sym in R.shared.L.SYMS:
                R.shared.L.SYMS.remove(sym)
        before_basket = {k: list(v) for k, v in R.shared.MEMBERS.items()}
        with patch.object(R, 'extra_universe', return_value=rows):
            added = R.install_extra_universe()
        self.assertEqual(sorted(added), ['GRSE(DEF)', 'PARAS(DEF)'])
        self.assertIn('GRSE', R.shared.L.SYMS)
        self.assertEqual(R.shared.SECTOR_OF['GRSE'], 'DEF')
        self.assertEqual({k: list(v) for k, v in R.shared.MEMBERS.items()}, before_basket)
        for sym in ('GRSE', 'PARAS'):
            R.shared.L.SYMS.remove(sym)
            R.shared.SECTOR_OF.pop(sym, None)

    def test_extra_universe_can_be_disabled(self):
        with patch.dict(os.environ, {'M16_EXTRA_UNIVERSE': '0'}):
            self.assertEqual(R.extra_universe(), [])

    def test_shipped_extra_universe_covers_the_defence_names(self):
        names = {sym for sym, _ in R.extra_universe()}
        self.assertTrue({'GRSE', 'PARAS', 'DATAPATTNS', 'ASTRAMICRO', 'BEML'} <= names)

    def test_shipped_extra_universe_is_broad_and_never_duplicates_the_fno_list(self):
        rows = R.extra_universe()
        fno = {r['symbol'] for r in csv.DictReader((R.ROOT / 'fno_universe.csv').open())}
        names = [sym for sym, _ in rows]
        self.assertGreaterEqual(len(rows), 150)                 # all major sectors
        self.assertEqual(len(names), len(set(names)))           # no duplicate symbols
        self.assertEqual(set(names) & fno, set())               # never re-add an F&O name
        self.assertTrue(all(sym == sym.upper() and sector for sym, sector in rows))
        self.assertTrue(all(re.fullmatch(r'[A-Z0-9&-]{2,20}', sym) for sym in names))
        sectors = {sector for _, sector in rows}
        self.assertTrue({'AUTO', 'BANK', 'CAPGOODS', 'CHEMICALS', 'DEF', 'FIN', 'IT', 'MEDIA',
                         'MET', 'PHA', 'REALTY', 'TEXTILE'} <= sectors)
        self.assertGreaterEqual(len(sectors), 25)

    def test_every_documented_sector_label_is_lower_case_clean_and_known(self):
        baskets = {r['sector'] for r in csv.DictReader((R.ROOT / 'fno_sector_map.csv').open())}
        extra_only = {'TEXTILE', 'MEDIA', 'SUGAR', 'SHIPPING'}
        labels = {sector for _, sector in R.extra_universe()}
        self.assertEqual(labels - baskets, extra_only)

    def test_extra_without_history_is_dormant_and_fno_names_are_not(self):
        cache = {sym: dict(ok=True, last_day='2026-11-26') for sym in R.shared.L.SYMS if sym in ('AXISBANK', 'SBIN')}
        cache['GRSE'] = dict(ok=False)
        cache['TITAN'] = dict(ok=True, last_day='2026-11-26')
        dormant = R.inactive_symbols(cache)
        self.assertIn('GRSE', dormant)          # cannot seed without prior-day history
        self.assertNotIn('AXISBANK', dormant)
        self.assertNotIn('TITAN', dormant)

    def test_bootstrap_fetches_the_extra_universe_too(self):
        import live_runner
        names = live_runner.extra_universe_symbols()
        self.assertGreaterEqual(len(names), 150)
        self.assertIn('GRSE', names)
        fno = {r['symbol'] for r in csv.DictReader((R.ROOT / 'fno_universe.csv').open())}
        self.assertEqual(set(names) & fno, set())     # never re-fetch an F&O name
        with patch.dict(os.environ, {'M16_EXTRA_UNIVERSE': '0'}):
            self.assertEqual(live_runner.extra_universe_symbols(), [])

    def test_extra_sector_without_a_basket_says_unverified(self):
        gate = dict(status='unknown', sector='MEDIA', reason='MEDIA not in the persistence board')
        line, _ = R.sector_context_lines(gate)
        self.assertIn('MEDIA not in the persistence board', line)
        self.assertIn('sector unverified', line)


class ContinuationTests(unittest.TestCase):
    def test_manual_recovery_at_legacy_cap_preserves_state_and_automatic_cap(self):
        st = M.fresh_state(DAY)
        st.update(session_starts=12, events=[{'id': 'already-seen'}], cursors={'TEST': at('10:50').isoformat()})
        before = copy.deepcopy(st)
        with patch.dict(os.environ, {'M16_MAX_SESSION_STARTS': '12'}):
            with self.assertRaisesRegex(SystemExit, 'automatic restart cap'):
                R.register_session_start(st, automatic=True)
            self.assertEqual(st, before)
            R.register_session_start(st, automatic=False)
            self.assertEqual(st['session_starts'], 13)
            self.assertEqual(st['automatic_session_starts'], 12)
            self.assertEqual(st['events'], before['events'])
            self.assertEqual(st['cursors'], before['cursors'])
            with self.assertRaises(SystemExit):
                R.register_session_start(st, automatic=True)

    def test_manual_starts_do_not_consume_automatic_allowance(self):
        st = M.fresh_state(DAY)
        with patch.dict(os.environ, {'M16_MAX_SESSION_STARTS': '2'}):
            R.register_session_start(st, automatic=False)
            R.register_session_start(st, automatic=True)
            R.register_session_start(st, automatic=False)
            R.register_session_start(st, automatic=True)
            self.assertEqual(st['session_starts'], 4)
            self.assertEqual(st['automatic_session_starts'], 2)
            with self.assertRaises(SystemExit): R.register_session_start(st, automatic=True)

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

    def test_converging_seeds_with_different_pullback_bars_alert_once(self):
        st, key = armed()
        second = copy.deepcopy(st['setups'][key])
        second.update(id=key + ':later-seed', signal='BUY-EX', signal_bar=at('09:35').isoformat(),
                      pullback_bar=at('09:40').isoformat())
        st['setups'][second['id']] = second
        M.running_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:52'))
        self.assertEqual(len(st['events']), 1)
        self.assertEqual(len(st['outbox']), 1)
        self.assertEqual(len(st['events'][0]['coincident_original_signals']), 1)
        M.closed_bar(st, 'AXISBANK', bar('09:50', h=112, l=108, c=111), at('09:55:01'))
        self.assertEqual([e['kind'] for e in st['events']], ['RUNNING_BREAK', 'CLOSE_CONFIRMED'])
        self.assertEqual(len(st['outbox']), 2)

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
        with patch.dict(os.environ, {'M16_EMA_TOLERANCE_PCT': '0'}):
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

    def test_shallow_ema_undercut_is_tolerated_and_recorded(self):
        # BDL 2026-11-27 11:15: close 1082.30 vs EMA9 1084.40 = 0.19% undercut.
        for sell in (False, True):
            st, key = armed(sell)
            b = bar('09:50', o=108, h=109, l=107, c=107.9, e9=108.0, e10=107.95)
            if sell:
                b = invert(b)
            M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
            self.assertEqual(st['setups'][key]['phase'], 'armed')
            self.assertAlmostEqual(st['setups'][key]['ema_undercut'], 0.1, places=1)

    def test_a_real_ema_break_still_invalidates(self):
        st, key = armed()
        b = bar('09:50', o=108, h=109, l=107, c=106.0, e9=108.0, e10=107.9)  # 1.9% under
        M.closed_bar(st, 'AXISBANK', b, at('09:55:01'))
        self.assertEqual(st['setups'][key]['phase'], 'invalid')

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
        rows[3]['ema9'] = 111.0     # a real break, not pullback noise
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

    def event(self, price=111.5, kind='CLOSE_CONFIRMED', bos=111.):
        return dict(id='E1', symbol='AXISBANK', side='BUY', kind=kind,
                    bar=at('09:50').isoformat(), observed=at('09:55:03').isoformat(),
                    price=price, bos=bos, pullback='FIB_0.236_0.382',
                    seed=at('09:45').isoformat(), signal='BUY-EX',
                    sector_gate={})

    def board_state(self, event, observed='09:55:22'):
        st = M.fresh_state(DAY)
        st['sector_gate_required'] = True
        st['sector_snapshots'] = [dict(close=at('09:55').isoformat(),
                                       observed=at(observed).isoformat(),
                                       sectors={'BANK': dict(persist_bull=90., persist_bear=10.)},
                                       complete=True, missing_sectors=[], missing_members=[])]
        st['outbox']['E1'] = dict(event=event, status='pending', awaiting_sector=True)
        return st

    def test_board_publication_wait_extends_only_the_close_window(self):
        event = self.event()
        self.assertTrue(R.event_fresh(event, at('09:55:25')))
        self.assertFalse(R.event_fresh(event, at('09:55:35')))
        event['board_wait'] = 22.
        self.assertTrue(R.event_fresh(event, at('09:55:45')))
        self.assertFalse(R.event_fresh(event, at('09:56:00')))
        future = self.event()
        future['observed'] = at('09:55:30').isoformat()
        self.assertFalse(R.event_fresh(future, at('09:55:25')))  # observation cannot be from the future

    def test_awaiting_board_is_not_expired_inside_the_wait_cap(self):
        event = self.event()
        st = self.board_state(event, observed='09:50')
        with gate_mode(), patch.object(R.m16_sector, 'decision', return_value={
                'status': 'unknown', 'reason': 'sector board unavailable'}), patch.object(
                R, 'save'), patch.object(R, 'telegram_targets', return_value=[('t', 'c')]), patch.object(
                R.tg, 'send_message_to') as send:
            R.flush(st, at('09:55:50'))
            send.assert_not_called()
        self.assertEqual(st['outbox']['E1']['status'], 'pending')

    def test_advisory_mode_never_waits_for_the_board(self):
        event = self.event()
        st = self.board_state(event, observed='09:50')
        with patch.object(R.m16_sector, 'decision', return_value={
                'status': 'unknown', 'reason': 'sector board stale'}), patch.object(
                R, 'save'), patch.object(R, 'telegram_targets', return_value=[('t', 'c')]), patch.object(
                R.tg, 'send_message_to', return_value=True) as send:
            R.flush(st, at('09:55:10'))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(st['outbox']['E1']['status'], 'sent')
        self.assertEqual(st['outbox']['E1']['event']['sector_gate_at_send']['status'], 'unknown')
        self.assertIn('sector unverified', send.call_args.args[2])

    def test_board_publication_wait_promotes_and_sends(self):
        event = self.event(price=111.5)
        st = self.board_state(event)
        with gate_mode(), patch.object(R.m16_sector, 'decision', return_value={
                'status': 'pass', 'sector': 'BANK', 'score': 90., 'close': at('09:55').isoformat(),
                'observed': at('09:55:22').isoformat(), 'top3': ['BANK'],
                'known_rank': 1, 'worst_rank': 1, 'missing_sectors': []}), patch.object(
                R, 'save'), patch.object(R, 'telegram_targets', return_value=[('t', 'c')]), patch.object(
                R.tg, 'send_message_to', return_value=True) as send:
            R.flush(st, at('09:55:48'))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(st['outbox']['E1']['status'], 'sent')
        self.assertAlmostEqual(st['outbox']['E1']['event']['board_wait'], 22., places=1)

    def test_extended_window_still_rejects_a_price_that_left_the_level(self):
        event = self.event(price=113.)          # +1.8% beyond a 111 BOS
        st = self.board_state(event)
        with patch.object(R.m16_sector, 'decision', return_value={
                'status': 'pass', 'sector': 'BANK', 'score': 90., 'close': at('09:55').isoformat(),
                'observed': at('09:55:22').isoformat(), 'top3': ['BANK'],
                'known_rank': 1, 'worst_rank': 1, 'missing_sectors': []}), patch.object(
                R, 'save'), patch.object(R, 'telegram_targets', return_value=[('t', 'c')]), patch.object(
                R.tg, 'send_message_to') as send:
            R.flush(st, at('09:55:48'))
            send.assert_not_called()
        self.assertEqual(st['outbox']['E1']['status'], 'expired_unsent')

    def test_expired_close_still_reaches_the_delayed_path(self):
        st = M.fresh_state(DAY)
        st['sector_gate_required'] = True
        event = self.event(price=111.4)
        event['live'] = False
        st['events'].append(event)
        st['outbox']['E1'] = dict(event=event, status='expired_unsent')
        rows = [(dict(close=111.4), None, '', {})]
        with patch.object(R.m16_sector, 'decision', return_value={
                'status': 'pass', 'sector': 'BANK', 'score': 90., 'close': at('09:55').isoformat()}):
            R.queue_late_entries(st, 'AXISBANK', rows, at('10:05'))
        self.assertEqual(st['outbox']['E1']['status'], 'pending')
        self.assertIn('late', st['outbox']['E1']['event'])

    def test_sent_close_is_never_requeued_by_the_delayed_path(self):
        st = M.fresh_state(DAY)
        st['sector_gate_required'] = True
        event = self.event(price=111.4)
        event['live'] = True
        st['events'].append(event)
        st['outbox']['E1'] = dict(event=event, status='sent')
        rows = [(dict(close=111.4), None, '', {})]
        with patch.object(R.m16_sector, 'decision', return_value={
                'status': 'pass', 'sector': 'BANK', 'score': 90., 'close': at('09:55').isoformat()}):
            R.queue_late_entries(st, 'AXISBANK', rows, at('10:05'))
        self.assertEqual(st['outbox']['E1']['status'], 'sent')
        self.assertNotIn('late', st['outbox']['E1']['event'])

    def test_board_publication_lag_is_credited_at_detection(self):
        with gate_mode():
            self._board_publication_lag_is_credited_at_detection()

    def _board_publication_lag_is_credited_at_detection(self):
        import contextlib, io
        st, _ = armed()
        st['sector_gate_required'] = True
        st['sector_snapshots'] = [dict(close=at('09:55').isoformat(),
                                       observed=at('09:55:22').isoformat(),
                                       sectors={'BANK': dict(persist_bull=90., persist_bear=10.)},
                                       complete=True, missing_sectors=[], missing_members=[])]
        R.apply(st, 'AXISBANK', [(bar('09:50', h=111, l=108, c=110.5, e9=109, e10=108), None, '', [])],
                at('09:55:33'))          # 33s after the close, board took 22s of it
        event = st['events'][0]
        self.assertTrue(event['live'])
        self.assertAlmostEqual(event['board_wait'], 22., places=1)
        self.assertEqual(len(st['outbox']), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            R.flush(st, at('09:55:33'), True)
        self.assertIn('CANDLE CLOSE', out.getvalue())
        self.assertNotIn('DELAYED', out.getvalue())

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
        with patch.object(R.m16_sector, 'warm', return_value=(64,19)), patch.object(R.m16_sector, 'refresh', side_effect=ValueError('offline test')), patch.object(R.m16_sector, 'decision', return_value={'status': 'pass', 'sector': 'BANK', 'score': 75., 'close': at('09:50').isoformat()}), patch.object(R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
            R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(R, 'timed_fetch', side_effect=fetch), patch.object(
            R, 'analyze', side_effect=analyze), patch.object(R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
            R.time, 'sleep', side_effect=sleep), patch.object(R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 1, dry_run=True)
        return st

    def test_live_workers_use_spawn_instead_of_forking_fetch_threads(self):
        with patch.object(R.multiprocessing, 'get_context', wraps=R.multiprocessing.get_context) as context:
            self.run_snapshot_session(refresh_after_close=True)
        self.assertEqual([call.args for call in context.call_args_list], [('spawn',), ('spawn',), ('spawn',)])

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

    def test_zero_scan_code_not_logged_and_dormant_excluded_from_health(self):
        rows = R.normalize(pd.DataFrame([bar('09:30'), bar('09:35')]), DAY)
        codes = iter([0, 111])
        def engine(frame, params):
            c = next(codes)
            return pd.DataFrame({'scan_code': [c], 'scan_name': ['NONE' if c == 0 else 'BUY-EX11']}, index=[frame.index[-1]])
        with patch.object(R.scanner, 'run_symbol', side_effect=engine):
            sym, output = R.analyze(('A', rows, {'ok': True, 'eng_hist': None}, None, at('09:40:01')))
        self.assertIsNone(output[0][1])
        self.assertEqual(output[1][1], 111)
        st = M.fresh_state(DAY)
        R.apply(st, sym, output, at('09:40:01'))
        self.assertEqual(len(st['signal_audit']), 1)
        self.assertEqual(st['signal_audit'][0]['code'], 111)
        st['inactive_symbols'] = ['ICICIPRULI']
        st['feed_health'] = {'A': dict(ok=True, at=at('09:40:01').isoformat(), latest=at('09:35').isoformat())}
        with patch.object(R.shared.L, 'SYMS', ['A', 'ICICIPRULI']):
            R.notify_health(st, at('09:40:01'), dry_run=True)
        self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_partial_sector_board_retries_and_upgrades_in_place(self):
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
        st = M.fresh_state(DAY)
        clock, elapsed = [at('09:55:02')], [0.]
        steps = iter([at('09:55:05'), at('09:55:08'), at('15:31:01')])
        partial = dict(close=at('09:55').isoformat(), observed=at('09:55:02').isoformat(),
                       sectors={'A': dict(persist_bull=90, persist_bear=10)},
                       complete=False, missing_sectors=['CEMENT'], missing_members=['SHREECEM'])
        complete = dict(close=at('09:55').isoformat(), observed=at('09:55:05').isoformat(),
                        sectors={'A': dict(persist_bull=90, persist_bear=10),
                                 'CEMENT': dict(persist_bull=75, persist_bear=25)},
                        complete=True, missing_sectors=[], missing_members=[])
        def sleep(_):
            clock[0] = next(steps)
            elapsed[0] += 3.
        with patch.object(R.m16_sector, 'warm', return_value=(63, 19)), patch.object(
                R.m16_sector, 'refresh', side_effect=[partial, complete]) as ref, patch.object(
                R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(
                R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', []), patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {}, 1, dry_run=True)
        self.assertEqual(ref.call_count, 2)
        self.assertEqual(len(st['sector_snapshots']), 1)
        self.assertTrue(st['sector_snapshots'][0]['complete'])
        self.assertEqual(len(st['sector_snapshots'][0]['sectors']), 2)

    def test_closing_gap_audit_throttle_and_eod_final_bar_grace(self):
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
        bars_1510 = R.normalize(pd.DataFrame([bar('15:10')]), DAY)
        bars_1525 = R.normalize(pd.DataFrame([bar('15:10'), bar('15:25')]), DAY)
        # Once checked after 35s into 15:20 or 15:25 bucket, omitted 15:15/15:20
        # must not spin-poll every 5s, while 15:30:01 resumes polling for 15:25.
        self.assertTrue(R.needs_audit_refresh((bars_1510, at('15:20:05')), at('15:15'), at('15:20:10')))
        self.assertFalse(R.needs_audit_refresh((bars_1510, at('15:20:40')), at('15:15'), at('15:22:00')))
        self.assertFalse(R.needs_audit_refresh((bars_1510, at('15:25:40')), at('15:20'), at('15:28:00')))
        self.assertTrue(R.needs_audit_refresh((bars_1510, at('15:25:40')), at('15:25'), at('15:30:05')))
        # Live session waits past 15:31:00 (up to 15:32:30) for slow-publishing
        # 15:25 closing bars before writing the EOD report, and marks post-cutoff
        # sector_health inactive rather than echoing stale pre-cutoff errors.
        st, _ = armed()
        st['setups'][next(iter(st['setups']))]['phase'] = 'expired'
        st['cursors']['AXISBANK'] = at('15:10').isoformat()
        st['sector_health'] = dict(ok=False, reason='stale error at 12:35')
        clock, elapsed = [at('15:31:05')], [0.]
        steps = iter([at('15:31:20'), at('15:31:25')])
        def fetch(sym):
            if clock[0] < at('15:31:15'):
                raise ValueError('15:25 not published yet')
            return bars_1525, clock[0]
        def analyze(job):
            return job[0], [(bar('15:25'), None, '', {})]
        def sleep(_):
            clock[0] = next(steps)
            elapsed[0] += 6.
        finished_cursors = {}
        def record_finish(state, dry_run=False):
            finished_cursors.update(state['cursors'])
            state['eod_done'] = True
        with patch.object(R.m16_sector, 'warm', return_value=(63, 19)), patch.object(
                R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(
                R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
                R, 'timed_fetch', side_effect=fetch), patch.object(
                R, 'analyze', side_effect=analyze), patch.object(
                R, 'notify_health'), patch.object(
                R, 'finish', side_effect=record_finish) as fin, patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 5, dry_run=False)
        fin.assert_called_once()
        self.assertEqual(finished_cursors['AXISBANK'], at('15:25').isoformat())
        self.assertIn(at('15:25').isoformat(), st['audit_bars']['AXISBANK'])
        self.assertEqual(st['sector_health']['phase'], 'post_cutoff')
        self.assertFalse(st['sector_health']['active'])

    def test_startup_in_flight_wave_defers_false_degraded_notice(self):
        from concurrent.futures import Future
        pending_fetches = []
        class DeferredFetchPool:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                pending_fetches.append((f, fn, args))
                return f
        class ImmediatePool:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                f.set_result(fn(*args))
                return f
        st = M.fresh_state(DAY)
        st['history_health'] = {'AXISBANK': True}
        clock, elapsed = [at('09:46:14')], [0.]
        candle = bar('09:40')
        frame = R.normalize(pd.DataFrame([candle]), DAY)
        frame.attrs['m16_feed'] = dict(latest=candle['dt'])
        def fetch(sym):
            return frame, clock[0]
        def analyze(job):
            return job[0], [(candle, None, '', {})]
        def sleep(_):
            while pending_fetches:
                f, fn, args = pending_fetches.pop(0)
                f.set_result(fn(*args))
            elapsed[0] += 3.
            if elapsed[0] >= 9.:
                elapsed[0] = 70.
        board = dict(close=at('09:45').isoformat(), observed=at('09:46:14').isoformat(),
                     sectors={'BANK': dict(persist_bull=80., persist_bear=20.)},
                     meta='ok', complete=True, missing_sectors=[])
        with patch.object(R.m16_sector, 'warm', return_value=(63, 19)), patch.object(
                R.m16_sector, 'refresh', return_value=board), patch.object(
                R.m16_sector, 'decision', return_value={'status': 'pass', 'sector': 'BANK', 'score': 80., 'close': at('09:45').isoformat()}), patch.object(
                R, 'ThreadPoolExecutor', DeferredFetchPool), patch.object(
                R, 'ProcessPoolExecutor', ImmediatePool), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
                R, 'timed_fetch', side_effect=fetch), patch.object(
                R, 'analyze', side_effect=analyze), patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 1, dry_run=True)
        self.assertNotIn('degraded', st.get('health_notices', {}))
        self.assertEqual(st['health_notice']['status'], 'healthy')

    def test_runtime_expiry_during_eod_grace_still_finishes_report(self):
        from concurrent.futures import Future
        class ImmediateExecutor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                f.set_result(fn(*args))
                return f
        st, _ = armed()
        st['cursors']['AXISBANK'] = at('15:10').isoformat()
        clock, elapsed = [at('15:31:10')], [0.]
        only_1510 = R.normalize(pd.DataFrame([bar('15:10')]), DAY)
        def sleep(_):
            # Simulate max_minutes expiring at 15:31:20 before 15:32:30 deadline
            clock[0] = at('15:31:20')
            elapsed[0] = 400.
        with patch.object(R.m16_sector, 'warm', return_value=(63, 19)), patch.object(
                R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(
                R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
                R, 'timed_fetch', return_value=(only_1510, at('15:31:10'))), patch.object(
                R, 'notify_health'), patch.object(
                R, 'finish') as fin, patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 1, dry_run=False)
        fin.assert_called_once_with(st, False)

    def test_opening_bell_waits_for_0920_first_close_without_false_outages(self):
        from concurrent.futures import Future
        class ImmediateExecutor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f
        st = M.fresh_state(DAY)
        clock, elapsed = [at('09:15:01')], [0.]
        steps = iter([at('09:17:00'), at('09:19:58'), at('09:20:02'), at('15:31:01')])
        phases_seen = []
        fetch_times = []
        first_bar = R.normalize(pd.DataFrame([bar('09:15')]), DAY)
        board = dict(close=at('09:20').isoformat(), observed=at('09:20:02').isoformat(),
                     sectors={'A': dict(persist_bull=90, persist_bear=10)},
                     complete=True, missing_sectors=[], missing_members=[])
        def sleep(_):
            phases_seen.append((clock[0].strftime('%H:%M:%S'), st.get('sector_health', {}).get('phase'), len(fetch_times)))
            clock[0] = next(steps)
            elapsed[0] += 6.
        def fake_fetch(sym):
            fetch_times.append((sym, clock[0].strftime('%H:%M:%S')))
            return first_bar, clock[0]
        with patch.object(R.m16_sector, 'warm', return_value=(63, 19)), patch.object(
                R.m16_sector, 'refresh', return_value=board) as ref, patch.object(
                R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(
                R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', ['AXISBANK']), patch.object(
                R, 'timed_fetch', side_effect=fake_fetch), patch.object(
                R, 'analyze', return_value=('AXISBANK', [])), patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {'AXISBANK': {'ok': True}}, 1, dry_run=True)
        # Zero fetches and zero sector refreshes during 09:15:01..09:19:58
        self.assertEqual(phases_seen[:3], [
            ('09:15:01', 'awaiting_first_close', 0),
            ('09:17:00', 'awaiting_first_close', 0),
            ('09:19:58', 'awaiting_first_close', 0),
        ])
        self.assertEqual(fetch_times, [('AXISBANK', '09:20:02')])
        ref.assert_called_once()
        self.assertEqual(st.get('feed_outages', {}), {})

    def test_discovery_rotation_covers_the_universe_every_three_buckets(self):
        syms = [f"S{i}" for i in range(9)]
        picked = [R.discovery_slice(syms, at(t), 3) for t in ('09:20', '09:25', '09:30')]
        self.assertEqual([len(p) for p in picked], [3, 3, 3])
        self.assertEqual(set().union(*picked), set(syms))
        self.assertEqual(sum(len(p) for p in picked), len(syms))
        self.assertEqual(R.discovery_slice(syms, at('09:35'), 3), picked[0])
        self.assertEqual(R.discovery_slice(syms, at('09:20'), 1), set(syms))
        self.assertEqual(R.discovery_slice(['A', 'B'], at('09:20'), 3), {'A', 'B'})

    def test_symbols_outside_the_slice_wait_for_their_turn(self):
        now = at('09:30:30')
        latest = {'A': (R.normalize(pd.DataFrame([bar('09:20')]), DAY), at('09:20:05'))}
        # No rotation (historical behaviour): everything stale is retried.
        self.assertEqual(R.discovery_retries(['A', 'B'], latest, now), {'A', 'B'})
        # Rotating: the due slice is retried, never-seen symbols wait their turn,
        # but a symbol that failed after being attempted is recovered at once.
        self.assertEqual(R.discovery_retries(['A', 'B'], latest, now, due={'A'}), {'A'})
        self.assertEqual(R.discovery_retries(['A', 'B'], latest, now, due=set()), set())
        self.assertEqual(R.discovery_retries(['A', 'B'], latest, now, due=set(),
                                             attempted={'B'}), {'B'})
        self.assertEqual(R.discovery_retries(['A', 'B'], latest, now, due={'C'},
                                             attempted={'B'}), {'B'})
        # A symbol holding usable-but-older bars waits for its slice: its closed
        # candles are analysed then, so nothing is lost and nothing floods.
        preclose = {'A': (R.normalize(pd.DataFrame([bar('09:45')]), DAY), at('09:54:59'))}
        self.assertEqual(R.discovery_retries(['A'], preclose, at('09:55:30'), due=set(),
                                             attempted={'A'}), set())
        self.assertEqual(R.discovery_retries(['A'], preclose, at('09:55:30'), due={'A'},
                                             attempted={'A'}), {'A'})

    def test_discovery_is_held_only_while_a_board_refresh_is_pending(self):
        with gate_mode(), patch.dict(os.environ, {'M16_SECTOR_RUNWAY_SECONDS': '20'}):
            self.assertTrue(R.discovery_hold(at('09:25:05'), 5, False, True, True))
            self.assertFalse(R.discovery_hold(at('09:25:05'), 5, True, True, True))
            self.assertFalse(R.discovery_hold(at('09:25:05'), 5, False, False, True))
            self.assertFalse(R.discovery_hold(at('09:25:05'), 5, False, True, False))
            self.assertFalse(R.discovery_hold(at('09:25:25'), 25, False, True, True))
            self.assertFalse(R.discovery_hold(at('12:40:05'), 5, False, True, True))

    def test_advisory_mode_keeps_the_board_courtesy_window_short(self):
        with patch.dict(os.environ, {'M16_SECTOR_RUNWAY_SECONDS': '20',
                                     'M16_SECTOR_RUNWAY_ADVISORY_SECONDS': '6'}):
            self.assertTrue(R.discovery_hold(at('09:25:04'), 4, False, True, True))
            self.assertFalse(R.discovery_hold(at('09:25:07'), 7, False, True, True))

    def test_rotation_is_not_reported_as_a_feed_outage(self):
        syms = ['A', 'B', 'C', 'D', 'E', 'F']
        now = at('09:55:30')
        due = {'A', 'B'}

        def build(current, trail):
            st = M.fresh_state(DAY)
            st['feed_health'] = {sym: dict(ok=True, at=at('09:55:00').isoformat(),
                                           latest=at(current if sym in due else trail).isoformat())
                                 for sym in syms}
            st['history_health'] = {sym: True for sym in syms}
            st['cursors'] = {sym: at(current if sym in due else trail).isoformat()
                             for sym in syms}
            return st

        st = build('09:55', '09:45')          # due symbols current, others one rotation behind
        with patch.object(R.shared.L, 'SYMS', syms), patch.dict(
                os.environ, {'M16_DISCOVERY_SLICES': '3'}):
            R.notify_health(st, now, True, due=due)
        self.assertEqual(st['health_notice']['status'], 'healthy',
                         dict(st['monitoring_health']['details']))

        st = build('09:30', '09:45')          # a due symbol falls behind
        with patch.object(R.shared.L, 'SYMS', syms), patch.dict(
                os.environ, {'M16_DISCOVERY_SLICES': '3'}):
            R.notify_health(st, now, True, due=due)
        self.assertEqual(st['health_notice']['status'], 'degraded')
        self.assertEqual(st['monitoring_health']['affected'], ['A', 'B'])

    def test_symbols_waiting_for_their_slice_are_not_flagged(self):
        syms = ['A', 'B', 'C', 'D', 'E', 'F']
        st = M.fresh_state(DAY)
        st['feed_health'] = {'A': dict(ok=True, at=at('09:25:00').isoformat(),
                                       latest=at('09:20').isoformat())}
        st['history_health'] = {'A': True}
        st['cursors'] = {'A': at('09:20').isoformat()}
        with patch.object(R.shared.L, 'SYMS', syms), patch.dict(
                os.environ, {'M16_DISCOVERY_SLICES': '3'}):
            R.notify_health(st, at('09:25:30'), True, due={'A'})
        # B..F have never been fetched yet and are simply not their turn.
        self.assertEqual(st['health_notice']['status'], 'healthy',
                         dict(st['monitoring_health']['details']))
        self.assertEqual(st['monitoring_health']['affected'], [])

    def test_first_bucket_discovers_one_rotation_slice(self):
        from concurrent.futures import Future

        class ImmediateExecutor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f

        syms = [f"S{i}" for i in range(9)]
        st = M.fresh_state(DAY)
        clock, elapsed = [at('09:20:00')], [0.]
        fetched = []
        frame = R.normalize(pd.DataFrame([bar('09:20')]), DAY)

        def fake_fetch(sym):
            fetched.append((sym, clock[0].strftime('%H:%M:%S')))
            return frame, clock[0]

        def sleep(_):
            clock[0] = clock[0] + pd.Timedelta(seconds=1)
            elapsed[0] += 1.

        with patch.dict(os.environ, {'M16_DISCOVERY_SLICES': '3'}), patch.object(
                R.m16_sector, 'warm', return_value=(3, 19)), patch.object(
                R.m16_sector, 'refresh', side_effect=ValueError('offline test')), patch.object(
                R, 'ThreadPoolExecutor', ImmediateExecutor), patch.object(
                R, 'ProcessPoolExecutor', ImmediateExecutor), patch.object(
                R.shared, 'now_ist', side_effect=lambda: clock[0]), patch.object(
                R.shared.L, 'SYMS', syms), patch.object(
                R, 'timed_fetch', side_effect=fake_fetch), patch.object(
                R, 'analyze', return_value=('S0', [])), patch.object(
                R.time, 'sleep', side_effect=sleep), patch.object(
                R.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(R, 'save'):
            R.live_session(st, {s: {'ok': True} for s in syms}, 1, dry_run=True)

        first_bucket = {s for s, t in fetched if t < '09:25:00'}
        self.assertEqual(len(first_bucket), 3, sorted(first_bucket))
        self.assertTrue(first_bucket.issubset(set(syms)))
        self.assertEqual(len(first_bucket | R.discovery_slice(syms, at('09:25'), 3)), 6)

    def test_readiness_check_verifies_next_session_without_mutating_state(self):
        import tempfile
        from pathlib import Path
        self.assertEqual(R.next_regular_session_day(pd.Timestamp('2026-11-26 18:10:00', tz='Asia/Kolkata')), '2026-11-27')
        self.assertEqual(R.next_regular_session_day(pd.Timestamp('2026-11-27 18:10:00', tz='Asia/Kolkata')), '2026-11-30')
        self.assertEqual(R.previous_regular_session_day('2026-11-27'), '2026-11-26')
        with tempfile.TemporaryDirectory() as tmp:
            fake_state = Path(tmp) / 'state16.json'
            prior = dict(M.fresh_state('2026-11-26'), eod_done=True, session_starts=21, automatic_session_starts=14)
            original_bytes = json.dumps(prior).encode()
            fake_state.write_bytes(original_bytes)
            with patch.object(R, 'STATE', fake_state), patch.object(
                    R.shared.L, 'SYMS', ['AXISBANK', 'ICICIPRULI']), patch.object(
                    R.shared, 'MEMBERS', {'BANK': ['AXISBANK'], 'INSURANCE': ['AXISBANK', 'ICICIPRULI']}), patch.object(
                    R.shared, 'symbol_history', side_effect=lambda sym, day: (
                        {'ok': True, 'last_day': '2026-10-22'} if sym == 'ICICIPRULI'
                        else {'ok': True, 'last_day': '2026-11-26'}
                    )):
                ok = R.readiness_check('2026-11-27')
            self.assertTrue(ok)
            self.assertEqual(fake_state.read_bytes(), original_bytes)

    def test_only_provider_rate_limit_failures_trigger_the_global_pause(self):
        for reason in ("no usable M16 feed: yahoo: HTTP 429 shared cooldown",
                       "no usable M16 feed: yahoo: empty response; host rate_limited",
                       "HTTP 429 too many requests"):
            self.assertTrue(R.provider_throttled(reason), reason)
        for reason in ("stale/missing completed candle expected=2026-11-26T09:25:00+05:30",
                       "no current-session candles", "empty response", ""):
            self.assertFalse(R.provider_throttled(reason), reason)

    def test_provider_cooldown_pauses_the_wave_instead_of_forcing_restarts(self):
        from concurrent.futures import Future
        class ImmediateExecutor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f

        syms = ["AXISBANK", "HDFCBANK", "ICICIBANK"]
        st = M.fresh_state(DAY)
        clock, elapsed = [at("09:25:00")], [0.]
        attempts = []

        def fake_fetch(sym):
            attempts.append((sym, elapsed[0]))
            raise ValueError("no usable M16 feed: yahoo: HTTP 429 shared cooldown")

        def sleep(_):
            elapsed[0] += 1.

        def advance(*_args):
            # One virtual second of market clock per loop iteration.
            clock[0] = clock[0] + pd.Timedelta(seconds=1)
            return clock[0]

        board = dict(close=at("09:25:00").isoformat(), observed=at("09:25:02").isoformat(),
                     sectors={"BANK": dict(persist_bull=90, persist_bear=10)},
                     complete=True, missing_sectors=[], missing_members=[])
        with patch.dict(os.environ, {"M16_PROVIDER_PAUSE_SECONDS": "10",
                                     "M16_PROVIDER_MAX_PAUSE_SECONDS": "120",
                                     "M16_UNHEALTHY_EXIT_SECONDS": "1"}), patch.object(
                R.m16_sector, "warm", return_value=(3, 19)), patch.object(
                R.m16_sector, "refresh", return_value=board), patch.object(
                R, "ThreadPoolExecutor", ImmediateExecutor), patch.object(
                R, "ProcessPoolExecutor", ImmediateExecutor), patch.object(
                R.shared, "now_ist", side_effect=advance), patch.object(
                R.shared.L, "SYMS", syms), patch.object(
                R, "timed_fetch", side_effect=fake_fetch), patch.object(
                R, "analyze", return_value=("AXISBANK", [])), patch.object(
                R.time, "sleep", side_effect=sleep), patch.object(
                R.time, "monotonic", side_effect=lambda: elapsed[0]), patch.object(R, "save"):
            # 1 virtual second per iteration for 60 iterations.
            R.live_session(st, {s: {"ok": True} for s in syms}, 1, dry_run=True)

        throttle = st.get("provider_throttle") or {}
        self.assertGreaterEqual(throttle.get("streak", 0), 2)
        self.assertEqual(throttle.get("pause_seconds"), 40.0)
        self.assertTrue(st.get("monitoring_health", {}).get("provider_throttled"))
        # Per-symbol backoff alone would retry each symbol at 0/2/6/14/30/60s.
        self.assertLessEqual(len(attempts), 12)
        self.assertGreaterEqual(len(attempts), 6)
        # Every admission happens only after the previous pause expired.
        self.assertEqual([t for _, t in attempts], sorted(t for _, t in attempts))
        self.assertLessEqual(len({t for _, t in attempts}), 4)

    def test_session_recovers_after_the_provider_cooldown_ends(self):
        from concurrent.futures import Future
        class ImmediateExecutor:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f

        syms = ["AXISBANK", "HDFCBANK", "ICICIBANK"]
        st = M.fresh_state(DAY)
        clock, elapsed, attempts = [at("09:25:00")], [0.], []
        frames = R.normalize(pd.DataFrame([bar("09:15"), bar("09:20")]), DAY)

        def fake_fetch(sym):
            elapsed[0] += 0.01
            attempts.append((sym, elapsed[0]))
            if len(attempts) <= 3:
                raise ValueError("no usable M16 feed: yahoo: HTTP 429 shared cooldown")
            return frames.copy(), clock[0]

        def sleep(_):
            elapsed[0] += 1.

        def advance(*_args):
            clock[0] = clock[0] + pd.Timedelta(seconds=1)
            return clock[0]

        board = dict(close=at("09:25:00").isoformat(), observed=at("09:25:02").isoformat(),
                     sectors={"BANK": dict(persist_bull=90, persist_bear=10)},
                     complete=True, missing_sectors=[], missing_members=[])
        with patch.dict(os.environ, {"M16_PROVIDER_PAUSE_SECONDS": "10",
                                     "M16_UNHEALTHY_EXIT_SECONDS": "1"}), patch.object(
                R.m16_sector, "warm", return_value=(3, 19)), patch.object(
                R.m16_sector, "refresh", return_value=board), patch.object(
                R, "ThreadPoolExecutor", ImmediateExecutor), patch.object(
                R, "ProcessPoolExecutor", ImmediateExecutor), patch.object(
                R.shared, "now_ist", side_effect=advance), patch.object(
                R.shared.L, "SYMS", syms), patch.object(
                R, "timed_fetch", side_effect=fake_fetch), patch.object(
                R, "analyze", return_value=("AXISBANK", [])), patch.object(
                R.time, "sleep", side_effect=sleep), patch.object(
                R.time, "monotonic", side_effect=lambda: elapsed[0]), patch.object(R, "save"):
            R.live_session(st, {s: {"ok": True} for s in syms}, 1, dry_run=True)

        # Cooldown was respected, then the session resumed and recorded success.
        self.assertGreaterEqual(st["provider_throttle"]["streak"], 1)
        self.assertTrue(st["feed_health"]["AXISBANK"]["ok"])
        self.assertGreater(len(attempts), 6)


class LateDiscoveryTests(unittest.TestCase):
    """A BOS found minutes late still alerts, priced at discovery, clearly labelled."""

    GATE_PASS = dict(status='pass', sector='BANK', score=90.0, known_rank=1, worst_rank=1,
                     missing_sectors=[], close='2026-11-20T09:55:00+05:30')
    GATE_BLOCK = dict(status='block', sector='BANK', score=40.0, known_rank=9, worst_rank=10,
                      missing_sectors=[], close='2026-11-20T09:55:00+05:30')
    GATE_UNKNOWN = dict(status='unknown', reason='sector board unavailable')

    def run_late(self, discovery_close, observed, gate=None):
        st, key = armed()
        st['sector_gate_required'] = True
        b = bar("09:50", o=108, h=max(111, discovery_close), l=108, c=discovery_close)
        rows = [dict(b, close=discovery_close, high=max(111, discovery_close))]
        with patch.object(R.m16_sector, 'decision', return_value=gate or dict(self.GATE_PASS)):
            R.apply(st, 'AXISBANK', [(rows[-1], 111, 'BUY-EX11', pd.DataFrame(rows))], at(observed))
        return st

    def test_two_minute_late_bos_alerts_at_the_discovery_price(self):
        st = self.run_late(110.5, "09:57:00")
        self.assertEqual(len(st['outbox']), 1)
        event = next(iter(st['outbox'].values()))['event']
        # No intrabar crossing was polled for this bar, so the close-only kind applies.
        self.assertEqual(event['kind'], 'CLOSE_ONLY')
        self.assertFalse(event['live'])
        self.assertAlmostEqual(event['late']['delay_minutes'], 2.0)
        self.assertAlmostEqual(event['late']['extension_pct'], 0.455, places=3)
        self.assertAlmostEqual(event['price'], 110.5)
        self.assertAlmostEqual(event['late']['original_price'], 110.5)
        text = R.alert_text(event)
        self.assertIn('DELAYED', text)
        self.assertIn('2.0 min late', text)
        self.assertIn('Entry level is the discovery price', text)

    def test_price_already_away_from_the_level_is_not_a_late_entry(self):
        for px in (112.0, 109.5):  # +1.8% (chased) and -0.45% (failed through BOS)
            st = self.run_late(px, "09:57:00")
            self.assertEqual(st['outbox'], {}, px)
            self.assertFalse(any(e.get('late') for e in st['events']))

    def test_late_window_and_cutoff_are_enforced(self):
        self.assertEqual(self.run_late(110.5, "10:20:00")['outbox'], {})  # 25 min late
        st, key = armed()
        st['sector_gate_required'] = True
        b = bar("12:26", o=108, h=111, l=108, c=110.5)
        with patch.object(R.m16_sector, 'decision', return_value=dict(self.GATE_PASS)):
            R.apply(st, 'AXISBANK', [(b, 111, 'BUY-EX11', pd.DataFrame([b]))], at("12:33:00"))
        self.assertEqual(st['outbox'], {})

    def test_late_entry_requires_a_validating_sector_board(self):
        with gate_mode():
            for gate in (self.GATE_BLOCK, self.GATE_UNKNOWN):
                self.assertEqual(self.run_late(110.5, "09:57:00", gate=gate)['outbox'], {}, gate['status'])

    def test_late_entry_is_delivered_in_advisory_mode(self):
        for gate in (self.GATE_BLOCK, self.GATE_UNKNOWN):
            st = self.run_late(110.5, "09:57:00", gate=gate)
            self.assertEqual(len(st['outbox']), 1, gate['status'])
            item = next(iter(st['outbox'].values()))
            self.assertEqual(item['status'], 'pending')
            self.assertFalse(item['event']['live'])          # labelled, never "live"
            self.assertTrue(item['event']['late'])
            self.assertIn('DELAYED', R.alert_text(item['event']))

    def test_disabled_late_alerts_and_replay_never_queue(self):
        st, key = armed()
        st['sector_gate_required'] = True
        b = bar("09:50", o=108, h=111, l=108, c=110.5)
        with patch.dict(os.environ, {'M16_LATE_ALERTS': '0'}), patch.object(
                R.m16_sector, 'decision', return_value=dict(self.GATE_PASS)):
            R.apply(st, 'AXISBANK', [(b, 111, 'BUY-EX11', pd.DataFrame([b]))], at("09:57:00"))
        self.assertEqual(st['outbox'], {})
        st2, _ = armed()
        st2['mode'] = 'offline_replay'
        with patch.object(R.m16_sector, 'decision', return_value=dict(self.GATE_PASS)):
            R.apply(st2, 'AXISBANK', [(b, 111, 'BUY-EX11', pd.DataFrame([b]))], at("09:57:00"))
        self.assertEqual(st2['outbox'], {})

    def test_running_break_is_never_reissued_as_a_delayed_alert(self):
        st, key = armed()
        st['sector_gate_required'] = True
        st['events'].append(dict(id='RUN:1', symbol='AXISBANK', side='BUY', kind='RUNNING_BREAK',
                                 bar=at('09:50').isoformat(), observed=at('09:52').isoformat(),
                                 price=110.5, bos=110.0, pullback='FIB', seed=at('09:30').isoformat(),
                                 signal='BUY-EX11', live=False, sector_gate={'status': 'unknown'}))
        b = bar("09:50", o=108, h=111, l=108, c=110.5)
        with patch.object(R.m16_sector, 'decision', return_value=dict(self.GATE_PASS)):
            R.apply(st, 'AXISBANK', [(b, 111, 'BUY-EX11', pd.DataFrame([b]))], at("09:58:00"))
        kinds = [item['event']['kind'] for item in st['outbox'].values()]
        self.assertNotIn('RUNNING_BREAK', kinds)

    def test_delayed_alert_freshness_runs_from_discovery_not_the_bos_bar(self):
        late = dict(delay_minutes=2.0, extension_pct=0.45, discovery_price=110.5, original_price=110.0)
        e = dict(kind='CLOSE_CONFIRMED', bar=at('09:50').isoformat(),
                 observed=at('09:57').isoformat(), late=late)
        self.assertTrue(R.event_fresh(e, at('09:57:20')))
        self.assertFalse(R.event_fresh(e, at('09:58:00')))


class OperationalHygieneTests(unittest.TestCase):
    def test_sector_pool_has_a_spare_worker_so_warmup_cannot_block_the_board(self):
        from concurrent.futures import Future
        seen = []

        class ImmediateExecutor:
            def __init__(self, **kw):
                seen.append(kw.get('max_workers'))
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def submit(self, fn, *args):
                f = Future()
                try:
                    f.set_result(fn(*args))
                except Exception as exc:
                    f.set_exception(exc)
                return f

        st = M.fresh_state(DAY)
        elapsed = [0.]
        board = dict(close=at("09:25:00").isoformat(), observed=at("09:25:02").isoformat(),
                     sectors={"BANK": dict(persist_bull=90, persist_bear=10)},
                     complete=True, missing_sectors=[], missing_members=[])
        with patch.dict(os.environ, {"M16_SECTOR_WORKERS": "2"}), patch.object(
                R.m16_sector, "warm", return_value=(1, 19)), patch.object(
                R.m16_sector, "refresh", return_value=board), patch.object(
                R, "ThreadPoolExecutor", ImmediateExecutor), patch.object(
                R, "ProcessPoolExecutor", ImmediateExecutor), patch.object(
                R.shared, "now_ist", return_value=at("09:25:00")), patch.object(
                R.shared.L, "SYMS", ["AXISBANK"]), patch.object(
                R, "timed_fetch", return_value=(R.normalize(pd.DataFrame([bar("09:15")]), DAY), at("09:25:00"))), patch.object(
                R, "analyze", return_value=("AXISBANK", [])), patch.object(
                R.time, "sleep", side_effect=lambda _: elapsed.__setitem__(0, elapsed[0] + 0.5)), patch.object(
                R.time, "monotonic", side_effect=lambda: elapsed[0]), patch.object(R, "save"):
            R.live_session(st, {"AXISBANK": {"ok": True}}, 0.02, dry_run=True)
        self.assertIn(2, seen)
        self.assertNotIn(1, seen)

    def test_health_notice_names_the_provider_throttle(self):
        st = M.fresh_state(DAY)
        st["provider_throttle"] = dict(at=at("09:59:00").isoformat(), streak=2, pause_seconds=20,
                                       reason="no usable M16 feed: yahoo: HTTP 429 shared cooldown")
        st["feed_health"] = {"AXISBANK": dict(ok=False, at=at("09:59:30").isoformat(),
                                              first_failure=at("09:59:00").isoformat(),
                                              failure_count=4, reason="HTTP 429 shared cooldown")}
        captured = {}

        def capture(token, chat, text):
            captured["text"] = text
            return True

        with patch.object(R, "telegram_targets", return_value=[("token", "chat")]), patch.object(
                R.tg, "send_message_to", side_effect=capture), patch.object(R, "save"):
            R.notify_health(st, at("10:00:00"), dry_run=False)
        self.assertIn("Provider rate limit", captured["text"])
        self.assertTrue(captured["text"].startswith("M16 FEED DEGRADED"))

    def test_an_extra_universe_gap_never_degrades_the_feed_notice(self):
        core = [sym for sym in R.shared.L.SYMS if sym not in {x for x, _ in R.extra_universe()}]
        st = M.fresh_state(DAY)
        st["feed_health"] = {sym: dict(ok=True, at=at("10:00:00").isoformat(),
                                       latest=at("09:55:00").isoformat()) for sym in core}
        st["feed_health"]["GRSE"] = dict(ok=False, at=at("09:58:00").isoformat(),
                                         first_failure=at("09:58:00").isoformat(),
                                         failure_count=3, reason="HTTP 404 no data")
        st["cursors"] = {sym: at("09:55:00").isoformat() for sym in core}
        captured = {}

        def capture(token, chat, text):
            captured["text"] = text
            return True

        with patch.object(R, "telegram_targets", return_value=[("token", "chat")]), patch.object(
                R.tg, "send_message_to", side_effect=capture), patch.object(R, "save"):
            R.notify_health(st, at("10:00:00"), dry_run=False)
        self.assertEqual(st["monitoring_health"]["affected"], [])
        self.assertNotIn("GRSE", st["monitoring_health"]["details"])
        self.assertFalse(str(captured.get("text", "")).startswith("M16 FEED DEGRADED"))

        # The same gap on a core F&O name does degrade the notice.
        st2 = M.fresh_state(DAY)
        st2["feed_health"] = {sym: dict(ok=True, at=at("10:00:00").isoformat(),
                                        latest=at("09:55:00").isoformat()) for sym in core}
        st2["feed_health"]["SBIN"] = dict(ok=False, at=at("09:58:00").isoformat(),
                                          first_failure=at("09:58:00").isoformat(), failure_count=3,
                                          reason="HTTP 500")
        st2["cursors"] = {sym: at("09:55:00").isoformat() for sym in core}
        with patch.object(R, "telegram_targets", return_value=[("token", "chat")]), patch.object(
                R.tg, "send_message_to", side_effect=capture), patch.object(R, "save"):
            R.notify_health(st2, at("10:00:00"), dry_run=False)
        self.assertIn("SBIN", st2["monitoring_health"]["affected"])

    def test_readiness_check_flags_schedule_and_gate_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".github/workflows").mkdir(parents=True)
            real = (Path(__file__).parent / ".github/workflows/16_live_m16.yml").read_text()
            drifted = real.replace('cron: "25 3 * * 1-5"', 'cron: "30 3 * * 1-5"').replace(
                'hhmm < "08:55"', 'hhmm < "09:00"')
            (root / ".github/workflows/16_live_m16.yml").write_text(drifted)
            state = root / "state16.json"
            state.write_text(json.dumps(dict(M.fresh_state("2026-11-26"), eod_done=True)))
            captured = {}
            with patch.object(R, "ROOT", root), patch.object(R, "STATE", state), patch.object(
                    R.shared.L, "SYMS", ["AXISBANK"]), patch.object(
                    R.shared, "MEMBERS", {"BANK": ["AXISBANK"]}), patch.object(
                    R.shared, "symbol_history", return_value={"ok": True, "last_day": "2026-11-26"}), patch(
                    "builtins.print", side_effect=lambda *a, **k: captured.setdefault("out", a[0] if a else "")):
                self.assertFalse(R.readiness_check("2026-11-27"))
            report = json.loads(captured["out"])
            issues = " ".join(report["workflow"]["issues"])
            self.assertIn("cron", issues)
            self.assertIn("08:55-15:40", issues)


if __name__ == '__main__':
    unittest.main()
