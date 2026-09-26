#!/usr/bin/env python3
"""EOD shadow selector from M5's paper ledger; no new orders or entry alerts.

The hypothesis is frozen: SELL-EX8 only, <=2, earliest observed first, one
per symbol, <=10 min signal-to-observation delay. Never rank by trade outcomes.
"""
import argparse
import json
from datetime import date
from pathlib import Path
from openpyxl import Workbook
import costs
import execution_audit

ROOT = Path(__file__).resolve().parent


def select(state, day):
    if state.get('date') != day:
        return []
    candidates = []
    for key, tr in state.get('trades', {}).items():
        a = tr.get('audit') or {}
        delay = a.get('delay_minutes')
        if (tr.get('signal') != 'SELL-EX8' or tr.get('side') != 'SELL'
                or delay is None or not 0 <= delay <= 10 or not a.get('observed_at')):
            continue
        candidates.append((a['observed_at'], tr.get('symbol', ''), key, tr))
    picked, symbols = [], set()
    for _, sym, key, tr in sorted(candidates):
        if sym in symbols:
            continue
        symbols.add(sym)
        picked.append(tr)
        if len(picked) == 2:
            break
    return picked


def build(day, selected, output):
    wb = Workbook(); sh = wb.active; sh.title = 'Shadow selector'
    sh.append(['M5 SELL-EX8 shadow; no independent fills; 2 max; observed delay <=10m'])
    sh.append(['Day','Symbol','Signal','Signal time','First observed IST','Delay min',
               'Signal close proxy ₹','Observed close proxy ₹','Status','M5 reported net ₹',
               'Final-only net ₹','Caution'])
    for tr in selected:
        a=tr['audit'];final=execution_audit.is_final(tr); net=costs.trade_costs(tr)['net']
        sh.append([day,tr['symbol'],tr['signal'],tr['time'],a['observed_at'],
                   a['delay_minutes'],tr['entry'],a.get('observation_price'),
                   'FINAL' if final else 'OPEN / UNVERIFIED',net,net if final else None,
                   'Reuses M5 hypothetical fill; observed close is not a broker quote'])
    sh.append(['SELECTION COUNT',len(selected)])
    sh.append(['FINAL-ONLY NET ₹',sum(costs.trade_costs(t)['net'] for t in selected
                                       if execution_audit.is_final(t))])
    sh.column_dimensions['E'].width=28;sh.column_dimensions['L'].width=66
    wb.save(output)
    return output


if __name__ == '__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--day',default=date.today().isoformat())
    ap.add_argument('--state',type=Path,default=ROOT/'state5.json')
    args=ap.parse_args()
    fp=args.state
    st=json.loads(fp.read_text()) if fp.exists() else {}
    if st.get('date') != args.day or not st.get('eod_done'):
        raise SystemExit('M5 EOD state not available for requested date; refusing incomplete report')
    selected=select(st,args.day)
    path=ROOT/f'paper_selector_{args.day}.xlsx'
    build(args.day,selected,path)
    print(f'{path.name}: {len(selected)} shadow entries, '
          f'{sum(execution_audit.is_final(t) for t in selected)} final')
