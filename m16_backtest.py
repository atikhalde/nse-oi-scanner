"""Causal continuation replay of saved M16 candles/labels, never sends Telegram."""
import argparse
import ast
from collections import defaultdict
import json
from pathlib import Path
import pandas as pd
import m16_model as M


def replay_saved(audit):
    text = Path(audit).read_text(encoding='utf-8-sig')
    start = text.index('12. COMPLETE SAVED AUDIT')
    saved = ast.literal_eval(text[text.index('\n', start)+1:])
    st = M.fresh_state(saved['date'])
    labels = {(r['symbol'], r['bar']): r for r in saved['signal_audit']}
    contexts = defaultdict(list)
    for dt, sym, bar in sorted((dt, sym, b) for sym, bars in saved['audit_bars'].items() for dt, b in bars.items()):
        now = M.stamp(dt)+pd.Timedelta(minutes=5, seconds=1)
        M.closed_bar(st, sym, bar, now)
        contexts[sym].append(bar)
        row = labels.get((sym, dt))
        if row and row.get('code') is not None:
            context = pd.DataFrame(contexts[sym][-24:])
            key = M.seed(st, sym, int(row['code']), row.get('signal', ''), bar, context)
            if key:
                M.direct_original_bos(st, key, bar, context, now)
        M.expire(st, now)
    return dict(date=saved['date'], symbols=len(saved['audit_bars']),
                method='Saved labels and EMA values; close+1s hypothetical availability; no intrabar reconstruction',
                technical_candidates=len(st['events']),
                movers_filter='removed; normal technical rules only',
                events=st['events'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--audit', required=True)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    result = replay_saved(args.audit)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != 'events'}, indent=2))
