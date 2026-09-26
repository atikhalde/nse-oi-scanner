#!/usr/bin/env python3
"""M15 independent opening-range reclaim paper runner; maximum three entries/day.

Requires a completed recent bar; cannot guarantee a minimum number of valid trades.
Usage: python m15_runner.py --loop 1
"""
import argparse
import json
import time
import pandas as pd
import live_runner as L
import feeds
import trader
import report
import telegram_bot as tg
import learn_log
from m15_entry import candidate

STATE = L.ROOT / 'state15.json'
MAX_TRADES = 3


def load(today):
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if state.get('date') == today:
            return state
    return {'date': today, 'trades': {}, 'alerts': [], 'seen': [],
            'skipped': [], 'eod_done': False, 'cycles': 0}


def save(state):
    STATE.write_text(json.dumps(state, indent=1))


def cycle():
    now = L.now_ist()
    today, clock = now.strftime('%Y-%m-%d'), now.strftime('%H:%M')
    st = load(today)
    if st['eod_done'] or clock < '09:50':
        return False
    bars_map = {}
    for sym in L.SYMS:
        try:
            b, _ = feeds.fetch_today(sym, L.SID[sym], now)
            if b is not None and not b.empty:
                b = b.sort_values('dt').drop_duplicates('dt').reset_index(drop=True)
                b['t'] = b.dt.dt.strftime('%H:%M')
                bars_map[sym] = b
        except Exception as exc:
            print(f'M15 feed {sym}: {exc}')
        time.sleep(0.15)

    # Revalue existing positions before scanning for new entries.
    for sym, old in list(st['trades'].items()):
        if sym not in bars_map:
            continue
        try:
            tr = trader.evaluate(sym, old['side'], old['time'], old['entry'],
                                 old['signal'], bars_map[sym],
                                 warmup=trader.load_warmup(L.HIST / f'{sym}.csv', today))
            if 'error' in tr:
                continue
            tr['setup'] = 'RANGE_RECLAIM'
            st['trades'][sym] = tr
            for ev in tr['events']:
                key = f"{sym}:{ev['key']}"
                if ev['key'] != 'ENTRY' and key not in st['alerts']:
                    st['alerts'].append(key)
                    save(st)
                    tg.send_message('🅼15 · ' + trader.fmt_alert(tr, ev['key']))
        except Exception as exc:
            print(f'M15 manage {sym}: {exc}')

    # Rank only *currently* completed bars, never backfill missed dispatches.
    picks = []
    for sym, b in bars_map.items():
        if sym in st['trades'] or len(b) < 8:
            continue
        for j in range(7, len(b)):
            bar_time = pd.Timestamp(b.dt.iloc[j])
            if bar_time.tzinfo is None:
                bar_time = bar_time.tz_localize(now.tz)
            age = (pd.Timestamp(now) - bar_time).total_seconds() / 60
            if not 5 <= age <= 22:  # 15-minute workflow cadence + scheduling margin
                continue
            key = f"{sym}:{b.t.iloc[j]}"
            if key in st['seen']:
                continue
            signal = candidate(b, j)
            st['seen'].append(key)
            if signal:
                picks.append((signal['score'], sym, j, signal))
    save(st)
    for _, sym, j, signal in sorted(picks, key=lambda p: (-p[0], p[1])):
        if len(st['trades']) >= MAX_TRADES:
            st['skipped'].append({'sym': sym, 'reason': 'daily cap'})
            continue
        b = bars_map[sym]
        entry_time = str(b.t.iloc[j])
        tr = trader.evaluate(sym, signal['side'], entry_time, float(b.close.iloc[j]),
                             signal['signal'], b,
                             warmup=trader.load_warmup(L.HIST / f'{sym}.csv', today))
        if 'error' in tr:
            st['skipped'].append({'sym': sym, 'reason': tr['error']})
            continue
        tr['setup'] = 'RANGE_RECLAIM'
        st['trades'][sym] = tr
        st['alerts'].append(f'{sym}:ENTRY')
        save(st)
        tg.send_message('🅼15 · ' + trader.fmt_alert(tr, 'ENTRY'))

    if clock >= '15:25':
        done = list(st['trades'].values())
        label = now.strftime('%d-%b-%Y') + ' (M15 range reclaim)'
        gate = {'status': 'M15 independent range reclaim',
                'source': 'completed recent 5-min bars; max three/day; no forced entries'}
        out = report.build(done, label, gate, str(L.ROOT / f'paper_test_M15_{today}.xlsx'))
        learn_log.harvest('M15', today, st, None, bars_map)
        st['eod_done'] = True
        save(st)
        tg.send_message('🅼15 EOD · ' + report.summary_text(done, label, gate))
        tg.send_document(out, caption=f'M15 paper report {today}')
    st['cycles'] += 1
    save(st)
    print(f"M15: {len(st['trades'])} entries; {len(bars_map)} symbols; {len(picks)} candidates")
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--loop', type=int, default=1)
    args = parser.parse_args()
    for i in range(max(1, args.loop)):
        if not cycle():
            break
        if i < args.loop - 1:
            time.sleep(240)
